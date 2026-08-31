"""上传会话服务：创建/查询/完成/中止。

完成事务不变量：MinIO 合并成功后，在同一个数据库事务中创建
资产版本 + Job + Outbox 并把会话标记为 COMPLETED；
完成接口幂等——重复调用返回既有的版本与 Job，不产生重复记录。
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.assets.enums import AssetSource, AssetType, AssetVersionStatus
from app.assets.service import AssetService
from app.context import now_utc
from app.errors import conflict, not_found, validation_error
from app.ids import new_uuid7
from app.jobs.enums import JobType
from app.jobs.models import Job
from app.jobs.service import JobService
from app.settings import Settings
from app.uploads.minio import MinioAdapter
from app.uploads.models import UploadSession, UploadSessionStatus

logger = logging.getLogger(__name__)


def _sanitize_file_name(raw: str) -> str:
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."}:
        raise validation_error(f"文件名不合法：{raw!r}")
    return name[:512]


def _parse_acquired_at(
    properties: dict[str, Any], explicit: datetime | None = None
) -> datetime | None:
    """一等字段优先；仍接受 properties.acquired_at 以兼容旧客户端。"""
    if explicit is not None:
        if explicit.tzinfo is None or explicit.utcoffset() is None:
            raise validation_error("acquired_at 必须携带时区，例如 +08:00 或 Z")
        return explicit.astimezone(UTC)
    raw = properties.get("acquired_at")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise validation_error("properties.acquired_at 必须是 ISO8601 字符串")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise validation_error(f"properties.acquired_at 不是合法的 ISO8601 时间：{raw!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise validation_error("properties.acquired_at 必须携带时区，例如 +08:00 或 Z")
    return parsed.astimezone(UTC)


def _validate_uploaded_parts(
    uploaded_parts: list[dict[str, Any]], *, expected_count: int, expected_size: int
) -> None:
    """完成 Multipart 前校验分片集合与总字节数，禁止截断对象进入处理链路。"""
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
        asset_name: str,
        asset_type: AssetType,
        file_name: str,
        size_bytes: int,
        part_count: int,
        content_type: str | None,
        properties: dict[str, Any],
        source: AssetSource,
        asset_id: UUID | None = None,
        resource_catalog_id: UUID | None = None,
        satellite_id: UUID | None = None,
        sensor_id: UUID | None = None,
        acquired_at: datetime | None = None,
    ) -> tuple[UploadSession, list[dict[str, Any]]]:
        """创建逻辑资产（或追加到已有资产）+ 上传会话，并生成全部分片预签名 URL。"""
        if asset_type not in (AssetType.RASTER, AssetType.VECTOR, AssetType.ATTACHMENT):
            raise validation_error(f"资产类型 {asset_type.value} 不受支持")
        session_id = new_uuid7()
        safe_name = _sanitize_file_name(file_name)
        object_key = f"uploads/{session_id}/{safe_name}"
        properties = dict(properties)
        resolved_acquired = _parse_acquired_at(properties, acquired_at)
        if resolved_acquired is not None:
            properties.setdefault("acquired_at", resolved_acquired.isoformat())

        # 先完成全部依赖数据库/MinIO 之外状态的校验，再创建 Multipart：
        # 否则无效请求（如资源目录不存在）会留下无人 abort 的孤儿分片上传。
        self._assets.validate_asset_properties(asset_type, properties)
        if asset_id is not None:
            asset = self._assets.get_asset_required(asset_id)
            if asset.asset_type is not asset_type:
                raise validation_error(
                    f"不能把 {asset_type.value} 文件追加到 {asset.asset_type.value} 资产 {asset_id}"
                )
            if any(v is not None for v in (resource_catalog_id, satellite_id, sensor_id)):
                assigned = {
                    name
                    for name, value in (
                        ("resource_catalog_id", resource_catalog_id),
                        ("satellite_id", satellite_id),
                        ("sensor_id", sensor_id),
                    )
                    if value is not None
                }
                self._assets.update_asset(
                    asset.id,
                    resource_catalog_id=resource_catalog_id,
                    satellite_id=satellite_id,
                    sensor_id=sensor_id,
                    set_fields=assigned,
                )
                asset = self._assets.get_asset_required(asset.id)
        else:
            asset = self._assets.create_asset(
                name=asset_name,
                asset_type=asset_type,
                source=source,
                properties=properties,
                resource_catalog_id=resource_catalog_id,
                satellite_id=satellite_id,
                sensor_id=sensor_id,
            )

        upload_id = self._minio.create_multipart_upload(key=object_key, content_type=content_type)
        try:
            session = UploadSession(
                id=session_id,
                asset_id=asset.id,
                status=UploadSessionStatus.PENDING,
                minio_upload_id=upload_id,
                object_key=object_key,
                file_name=safe_name,
                size_bytes=size_bytes,
                part_count=part_count,
                content_type=content_type,
                properties=dict(properties),
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
            # 校验已通过但落库/预签名失败：尽力回收 Multipart，避免遗留孤儿上传
            try:
                self._minio.abort_multipart_upload(key=object_key, upload_id=upload_id)
            except Exception:
                logger.warning(
                    "孤儿 Multipart 上传清理失败，等待人工或生命周期策略清理",
                    extra={"object_key": object_key, "upload_id": upload_id},
                )
            raise
        return session, part_urls

    def get_session_required(self, session_id: UUID) -> UploadSession:
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

    def _lock_session(self, session_id: UUID) -> UploadSession:
        """行锁会话，使 complete 与 abort 互斥。"""
        session = self._session.scalar(
            sa.select(UploadSession).where(UploadSession.id == session_id).with_for_update()
        )
        if session is None:
            raise not_found("上传会话", session_id)
        return session

    def complete_session(self, session_id: UUID) -> dict[str, Any]:
        """完成上传：MinIO 合并 + 同事务创建版本/Job/Outbox。幂等。"""
        session = self._lock_session(session_id)
        if session.status is UploadSessionStatus.COMPLETED:
            return self._existing_completion(session)
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
            # 首次完成：合并分片；重复完成（NoSuchUpload）时对象已存在，走幂等分支
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

        # 同一事务：版本 + Job + Outbox + 会话状态。
        # 版本业务元数据取本批上传会话声明（session.properties），
        # 追加版本时不得从 DataAsset.properties 抄一份而丢弃本批元数据。
        asset = self._assets.get_asset_required(session.asset_id)
        properties = dict(session.properties)
        version = self._assets.create_version(
            asset_id=session.asset_id,
            original_file_name=session.file_name,
            size_bytes=int(existing_object["size"]),
            properties=properties,
            acquired_at=_parse_acquired_at(properties),
            status=AssetVersionStatus.VALIDATING,
        )
        job, _event = self._jobs.create_job_with_outbox(
            job_type=_job_type_for(asset.asset_type),
            asset_version_id=version.id,
            payload={
                "asset_version_id": str(version.id),
                "upload_session_id": str(session.id),
                "source_object_key": session.object_key,
                "file_name": session.file_name,
                "source_size_bytes": int(existing_object["size"]),
            },
        )
        session.status = UploadSessionStatus.COMPLETED
        session.completed_version_id = version.id
        session.completed_at = now_utc()
        self._session.flush()
        return {
            "session_id": str(session.id),
            "asset_id": str(session.asset_id),
            "asset_version_id": str(version.id),
            "job_id": str(job.id),
        }

    def _existing_completion(self, session: UploadSession) -> dict[str, Any]:
        """重复完成调用：返回既有版本与 Job（幂等）。"""
        version_id = session.completed_version_id
        if version_id is None:
            raise conflict(
                code="UPLOAD_SESSION_STATE_INVALID",
                detail=f"上传会话 {session.id} 状态为 COMPLETED 但缺少版本记录",
            )
        job_id = self._session.scalar(
            sa.select(Job.id)
            .where(Job.payload["asset_version_id"].astext == str(version_id))
            .limit(1)
        )
        return {
            "session_id": str(session.id),
            "asset_id": str(session.asset_id),
            "asset_version_id": str(version_id),
            "job_id": str(job_id) if job_id is not None else None,
        }

    def abort_session(self, session_id: UUID) -> UploadSession:
        """中止上传。必须先锁会话再改 MinIO，避免与 complete 交错把已完成会话写成 ABORTED。"""
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
        self._session.flush()
        return session


def _job_type_for(asset_type: AssetType) -> JobType:
    mapping = {
        AssetType.RASTER: JobType.RASTER_INGESTION,
        AssetType.VECTOR: JobType.VECTOR_INGESTION,
        AssetType.ATTACHMENT: JobType.ATTACHMENT_INGESTION,
    }
    return mapping[asset_type]
