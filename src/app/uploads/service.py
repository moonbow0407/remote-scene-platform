"""上传会话服务：创建/查询/完成/中止。

完成事务：MinIO 合并成功后，同一数据库事务把资产置为 VALIDATING 并创建 Job+Outbox。
完成接口幂等。分片数由服务端按文件大小计算。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.assets.enums import AssetStatus, AssetType
from app.assets.service import AssetService
from app.context import now_utc
from app.errors import conflict, not_found, validation_error
from app.ids import new_uuid7
from app.jobs.enums import JobType
from app.jobs.service import JobService
from app.settings import Settings
from app.uploads.minio import MinioAdapter
from app.uploads.models import UploadSession, UploadSessionStatus

logger = logging.getLogger(__name__)

_DEFAULT_PART_SIZE = 16 * 1024 * 1024
_MAX_PARTS = 10000
_RASTER_EXT = {".tif", ".tiff", ".gtiff", ".geotiff"}
_VECTOR_EXT = {".zip", ".geojson", ".gpkg", ".shp"}


def compute_part_count(size_bytes: int) -> int:
    if size_bytes <= _DEFAULT_PART_SIZE:
        return 1
    return min(math.ceil(size_bytes / _DEFAULT_PART_SIZE), _MAX_PARTS)


def infer_asset_type(file_name: str) -> AssetType | None:
    ext = Path(file_name).suffix.lower()
    if ext in _RASTER_EXT:
        return AssetType.RASTER
    if ext in _VECTOR_EXT:
        return AssetType.VECTOR
    if ext:
        return AssetType.ATTACHMENT
    return None


def _sanitize_file_name(raw: str) -> str:
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."}:
        raise validation_error(f"文件名不合法：{raw!r}")
    return name[:512]


def _validate_uploaded_parts(
    uploaded_parts: list[dict[str, Any]], *, expected_count: int, expected_size: int
) -> None:
    actual_numbers = {int(part["part_number"]) for part in uploaded_parts}
    expected_numbers = set(range(1, expected_count + 1))
    if actual_numbers != expected_numbers:
        missing = sorted(expected_numbers - actual_numbers)
        unexpected = sorted(actual_numbers - expected_numbers)
        raise conflict(
            code="UPLOAD_PARTS_INCOMPLETE",
            detail=f"上传分片不完整；缺少 {missing}，越界分片 {unexpected}",
        )
    actual_size = sum(int(part["size"]) for part in uploaded_parts)
    if actual_size != expected_size:
        raise conflict(
            code="UPLOAD_SIZE_MISMATCH",
            detail=f"已上传分片共 {actual_size} 字节，与登记大小 {expected_size} 不一致",
        )


def _job_type_for(asset_type: AssetType) -> JobType:
    return {
        AssetType.RASTER: JobType.RASTER_INGESTION,
        AssetType.VECTOR: JobType.VECTOR_INGESTION,
        AssetType.ATTACHMENT: JobType.ATTACHMENT_INGESTION,
    }[asset_type]


class UploadService:
    def __init__(self, session: Session, minio: MinioAdapter, settings: Settings) -> None:
        self._session = session
        self._minio = minio
        self._settings = settings
        self._assets = AssetService(session)
        self._jobs = JobService(session)

    def create_session(
        self,
        *,
        file_name: str,
        size_bytes: int,
        content_type: str | None,
        asset_type: AssetType | None,
    ) -> tuple[UploadSession, list[dict[str, Any]]]:
        safe_name = _sanitize_file_name(file_name)
        inferred = infer_asset_type(safe_name)
        resolved_type = asset_type or inferred
        if resolved_type is None:
            raise validation_error("无法从文件名判断类型，请显式传入 asset_type")
        if resolved_type not in (AssetType.RASTER, AssetType.VECTOR, AssetType.ATTACHMENT):
            raise validation_error(f"资产类型 {resolved_type.value} 不受支持")
        part_count = compute_part_count(size_bytes)
        object_key = f"uploads/{new_uuid7()}/{safe_name}"
        asset = self._assets.create_asset(
            name=safe_name,
            asset_type=resolved_type,
            original_file_name=safe_name,
            size_bytes=size_bytes,
            original_object_key=object_key,
        )
        upload_id = self._minio.create_multipart_upload(key=object_key, content_type=content_type)
        try:
            session = UploadSession(
                asset_id=asset.id,
                status=UploadSessionStatus.PENDING,
                minio_upload_id=upload_id,
                object_key=object_key,
                file_name=safe_name,
                size_bytes=size_bytes,
                part_count=part_count,
                content_type=content_type,
            )
            self._session.add(session)
            self._session.flush()
            part_urls = [
                {
                    "part_number": number,
                    "url": self._minio.presign_part_url(
                        key=object_key,
                        upload_id=upload_id,
                        part_number=number,
                        expires_in=self._settings.presign_expiry_seconds,
                    ),
                }
                for number in range(1, part_count + 1)
            ]
        except Exception:
            try:
                self._minio.abort_multipart_upload(key=object_key, upload_id=upload_id)
            except Exception:
                logger.warning(
                    "孤儿 Multipart 上传清理失败",
                    extra={"object_key": object_key, "upload_id": upload_id},
                )
            raise
        return session, part_urls

    def get_session_required(self, session_id: int) -> UploadSession:
        session = self._session.get(UploadSession, session_id)
        if session is None:
            raise not_found("上传会话", session_id)
        return session

    def presign_part(self, session: UploadSession, part_number: int) -> str:
        if not 1 <= part_number <= session.part_count:
            raise validation_error(f"分片编号必须在 1..{session.part_count} 内，收到 {part_number}")
        return self._minio.presign_part_url(
            key=session.object_key,
            upload_id=session.minio_upload_id,
            part_number=part_number,
            expires_in=self._settings.presign_expiry_seconds,
        )

    @property
    def presign_expiry_seconds(self) -> int:
        return self._settings.presign_expiry_seconds

    def list_parts(self, session: UploadSession) -> list[dict[str, Any]]:
        return self._minio.list_parts(key=session.object_key, upload_id=session.minio_upload_id)

    def _lock_session(self, session_id: int) -> UploadSession:
        session = self._session.scalar(
            sa.select(UploadSession).where(UploadSession.id == session_id).with_for_update()
        )
        if session is None:
            raise not_found("上传会话", session_id)
        return session

    def complete_session(self, session_id: int) -> dict[str, Any]:
        session = self._lock_session(session_id)
        if session.status is UploadSessionStatus.COMPLETED:
            return {"session_id": session.id, "asset_id": session.asset_id}
        if session.status is UploadSessionStatus.ABORTED:
            raise conflict(
                code="UPLOAD_SESSION_ABORTED", detail=f"上传会话 {session_id} 已中止，不能完成"
            )

        uploaded_parts = self._minio.list_parts(
            key=session.object_key, upload_id=session.minio_upload_id
        )
        existing_object = self._minio.head_object(key=session.object_key)
        if existing_object is None and not uploaded_parts:
            raise conflict(
                code="UPLOAD_SESSION_NO_PARTS",
                detail=f"上传会话 {session_id} 尚无任何已上传分片，无法完成",
            )
        if existing_object is None:
            _validate_uploaded_parts(
                uploaded_parts,
                expected_count=session.part_count,
                expected_size=session.size_bytes,
            )
            self._minio.complete_multipart_upload(
                key=session.object_key, upload_id=session.minio_upload_id, parts=uploaded_parts
            )
            existing_object = self._minio.head_object(key=session.object_key)
            if existing_object is None:
                raise conflict(
                    code="UPLOAD_SESSION_COMPLETE_FAILED",
                    detail=f"会话 {session_id} 合并后对象仍不存在，请检查分片完整性",
                )
        if int(existing_object["size"]) != session.size_bytes:
            raise conflict(
                code="UPLOAD_SIZE_MISMATCH",
                detail=(
                    f"合并对象共 {existing_object['size']} 字节，"
                    f"与登记大小 {session.size_bytes} 不一致"
                ),
            )

        asset = self._assets.get_asset_required(session.asset_id)
        asset.size_bytes = int(existing_object["size"])
        asset.original_object_key = session.object_key
        self._assets.set_status(asset, AssetStatus.VALIDATING)
        self._jobs.create_job_with_outbox(
            job_type=_job_type_for(asset.asset_type),
            asset_id=asset.id,
            payload={
                "asset_id": str(asset.id),
                "upload_session_id": str(session.id),
                "source_object_key": session.object_key,
                "file_name": session.file_name,
                "source_size_bytes": int(existing_object["size"]),
            },
        )
        session.status = UploadSessionStatus.COMPLETED
        session.completed_at = now_utc()
        self._session.flush()
        return {"session_id": session.id, "asset_id": session.asset_id}

    def abort_session(self, session_id: int) -> UploadSession:
        session = self._lock_session(session_id)
        if session.status is UploadSessionStatus.COMPLETED:
            raise conflict(
                code="UPLOAD_SESSION_COMPLETED", detail=f"上传会话 {session_id} 已完成，不能中止"
            )
        if session.status is UploadSessionStatus.ABORTED:
            return session
        self._minio.abort_multipart_upload(
            key=session.object_key, upload_id=session.minio_upload_id
        )
        session.status = UploadSessionStatus.ABORTED
        asset = self._assets.get_asset_by_id(session.asset_id)
        if asset is not None and asset.status is AssetStatus.UPLOADING:
            self._assets.set_status(
                asset,
                AssetStatus.FAILED,
                diagnostics={"reason": "UPLOAD_ABORTED", "detail": "上传已中止"},
            )
        self._session.flush()
        return session
