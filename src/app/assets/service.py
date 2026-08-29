"""资产服务：资产/版本/blob/工件的用例与状态转换。

事务边界：本服务不开启事务，由调用方（API 请求或 Worker 步骤）通过 session_scope
显式提交；跨步骤的幂等性由各步骤先行检查数据库现状保证。
"""

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from geoalchemy2 import WKTElement
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.assets.enums import (
    ArtifactKind,
    AssetSource,
    AssetType,
    AssetVersionStatus,
)
from app.assets.models import AssetArtifact, AssetVersion, DataAsset, ObjectBlob, RasterAssetVersion
from app.assets.version_state import is_version_transition_allowed
from app.context import ActorContext, get_actor
from app.errors import conflict, not_found
from app.ids import new_uuid7

logger = logging.getLogger(__name__)


class AssetService:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ---- 逻辑资产 ----

    def create_asset(
        self,
        *,
        name: str,
        asset_type: AssetType,
        source: AssetSource,
        properties: dict[str, Any] | None = None,
        actor: ActorContext | None = None,
    ) -> DataAsset:
        actor = actor or get_actor()
        asset = DataAsset(
            id=new_uuid7(),
            name=name,
            asset_type=asset_type,
            source=source,
            properties=properties or {},
            created_by=None if actor.actor_id is None else UUID(actor.actor_id),
        )
        self._session.add(asset)
        self._session.flush()
        return asset

    def get_asset(self, asset_id: UUID) -> DataAsset | None:
        return self._session.get(DataAsset, asset_id)

    def get_asset_required(self, asset_id: UUID) -> DataAsset:
        asset = self.get_asset(asset_id)
        if asset is None:
            raise not_found("资产", asset_id)
        return asset

    # ---- 版本 ----

    def create_version(
        self,
        *,
        asset_id: UUID,
        original_file_name: str,
        size_bytes: int,
        properties: dict[str, Any] | None = None,
        acquired_at: datetime | None = None,
        status: AssetVersionStatus = AssetVersionStatus.VALIDATING,
    ) -> AssetVersion:
        """创建不可变版本并指向为当前版本。

        并发说明：对父资产行加行锁后再取 max(version_number)，
        序列化同一资产的并发建版。
        """
        asset = self.get_asset_required(asset_id)
        self._session.execute(
            sa.select(DataAsset.id).where(DataAsset.id == asset_id).with_for_update()
        )
        max_number = self._session.scalar(
            sa.select(sa.func.max(AssetVersion.version_number)).where(
                AssetVersion.asset_id == asset_id
            )
        )
        version = AssetVersion(
            id=new_uuid7(),
            asset_id=asset_id,
            version_number=(max_number or 0) + 1,
            status=status,
            original_file_name=original_file_name,
            size_bytes=size_bytes,
            properties=properties or {},
            acquired_at=acquired_at,
        )
        self._session.add(version)
        self._session.flush()
        asset.current_version_id = version.id
        return version

    def get_version_required(self, asset_id: UUID, version_id: UUID) -> AssetVersion:
        version = self._session.get(AssetVersion, version_id)
        if version is None or version.asset_id != asset_id:
            raise not_found("资产版本", version_id)
        return version

    def get_version_by_id(self, version_id: UUID) -> AssetVersion | None:
        """按主键取版本（Worker 侧仅有版本 ID 时使用）。"""
        return self._session.get(AssetVersion, version_id)

    def set_version_status(
        self,
        version: AssetVersion,
        target: AssetVersionStatus,
        *,
        diagnostics: dict[str, Any] | None = None,
    ) -> AssetVersion:
        current = version.status
        if not is_version_transition_allowed(current, target):
            raise conflict(
                code="ASSET_VERSION_STATE_INVALID",
                detail=f"资产版本 {version.id} 不允许从 {current} 转换到 {target}",
            )
        version.status = target
        if diagnostics is not None:
            version.diagnostics = diagnostics
        return version

    # ---- 内容寻址对象 ----

    def get_or_create_blob(
        self, *, sha256: str, size_bytes: int, object_key: str
    ) -> tuple[ObjectBlob, bool]:
        """按 SHA-256 查找或创建 blob；命中即引用计数 +1（去重共享）。

        行锁防止并发哈希同一内容时重复插入/计数丢失。
        """
        blob_id = new_uuid7()
        created_id = self._session.scalar(
            insert(ObjectBlob)
            .values(
                id=blob_id,
                sha256=sha256,
                object_key=object_key,
                size_bytes=size_bytes,
                reference_count=1,
            )
            .on_conflict_do_nothing(index_elements=[ObjectBlob.sha256])
            .returning(ObjectBlob.id)
        )
        if created_id is not None:
            blob = self._session.get(ObjectBlob, created_id)
            assert blob is not None
            return blob, True

        existing = self._session.scalar(
            sa.select(ObjectBlob).where(ObjectBlob.sha256 == sha256).with_for_update()
        )
        if existing is None:
            raise RuntimeError(f"SHA-256 冲突处理后未找到 object_blob：{sha256}")
        if existing.size_bytes != size_bytes or existing.object_key != object_key:
            raise conflict(
                code="OBJECT_BLOB_METADATA_CONFLICT",
                detail=f"内容对象 {sha256} 的大小或对象键与既有记录不一致",
            )
        existing.reference_count += 1
        return existing, False

    def attach_blob(self, version: AssetVersion, blob: ObjectBlob) -> None:
        if version.blob_id is not None and version.blob_id != blob.id:
            raise conflict(
                code="ASSET_VERSION_BLOB_IMMUTABLE",
                detail=f"资产版本 {version.id} 已绑定其他 blob，不可覆盖",
            )
        version.blob_id = blob.id

    # ---- 栅格扩展与工件 ----

    def upsert_raster_ext(self, version_id: UUID, **fields: Any) -> RasterAssetVersion:
        """幂等写入栅格扩展元数据；不存在的字段不覆盖。"""
        ext = self._session.get(RasterAssetVersion, version_id)
        if ext is None:
            ext = RasterAssetVersion(asset_version_id=version_id)
            self._session.add(ext)
        for key, value in fields.items():
            if value is not None:
                setattr(ext, key, value)
        self._session.flush()
        return ext

    def get_raster_ext(self, version_id: UUID) -> RasterAssetVersion | None:
        return self._session.get(RasterAssetVersion, version_id)

    def upsert_artifact(
        self,
        *,
        version_id: UUID,
        kind: ArtifactKind,
        object_key: str,
        size_bytes: int | None = None,
        content_type: str | None = None,
    ) -> AssetArtifact:
        """按 (版本, 种类) 幂等写入工件；重复处理不产生重复行。"""
        existing = self._session.scalar(
            sa.select(AssetArtifact).where(
                AssetArtifact.asset_version_id == version_id, AssetArtifact.kind == kind
            )
        )
        if existing is not None:
            return existing
        artifact = AssetArtifact(
            id=new_uuid7(),
            asset_version_id=version_id,
            kind=kind,
            object_key=object_key,
            size_bytes=size_bytes,
            content_type=content_type,
        )
        self._session.add(artifact)
        self._session.flush()
        return artifact

    # ---- 检索 ----

    def search_versions(
        self,
        *,
        geometry_wkt: str | None = None,
        asset_type: AssetType | None = None,
        version_status: AssetVersionStatus | None = None,
        acquired_from: datetime | None = None,
        acquired_to: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[tuple[AssetVersion, DataAsset]], int]:
        """资产版本检索；geometry_wkt 为 EPSG:4326 WKT 时按 footprint 相交过滤。"""
        stmt = (
            sa.select(AssetVersion, DataAsset)
            .join(DataAsset, DataAsset.id == AssetVersion.asset_id)
            .outerjoin(RasterAssetVersion, RasterAssetVersion.asset_version_id == AssetVersion.id)
        )
        count_stmt = (
            sa.select(sa.func.count())
            .select_from(AssetVersion)
            .join(DataAsset, DataAsset.id == AssetVersion.asset_id)
            .outerjoin(RasterAssetVersion, RasterAssetVersion.asset_version_id == AssetVersion.id)
        )
        conditions: list[sa.ColumnElement[bool]] = []
        if geometry_wkt is not None:
            geom = WKTElement(geometry_wkt, srid=4326)
            conditions.append(sa.func.ST_Intersects(RasterAssetVersion.footprint, geom))
        if asset_type is not None:
            conditions.append(DataAsset.asset_type == asset_type)
        if version_status is not None:
            conditions.append(AssetVersion.status == version_status)
        if acquired_from is not None:
            conditions.append(AssetVersion.acquired_at >= acquired_from)
        if acquired_to is not None:
            conditions.append(AssetVersion.acquired_at <= acquired_to)
        if conditions:
            stmt = stmt.where(*conditions)
            count_stmt = count_stmt.where(*conditions)
        stmt = stmt.order_by(AssetVersion.created_at.desc()).offset(offset).limit(limit)
        rows = [(v, a) for v, a in self._session.execute(stmt)]
        total = int(self._session.scalar(count_stmt) or 0)
        return rows, total

    def list_artifacts(self, version_id: UUID) -> list[AssetArtifact]:
        return list(
            self._session.scalars(
                sa.select(AssetArtifact).where(AssetArtifact.asset_version_id == version_id)
            )
        )

    def find_artifact(self, version_id: UUID, kind: ArtifactKind) -> AssetArtifact | None:
        return self._session.scalar(
            sa.select(AssetArtifact).where(
                AssetArtifact.asset_version_id == version_id, AssetArtifact.kind == kind
            )
        )

    def get_artifact_required(self, version_id: UUID, kind: ArtifactKind) -> AssetArtifact:
        artifact = self._session.scalar(
            sa.select(AssetArtifact).where(
                AssetArtifact.asset_version_id == version_id, AssetArtifact.kind == kind
            )
        )
        if artifact is None:
            raise not_found("工件", kind.value)
        return artifact

    def resume_from_needs_input(self, version: AssetVersion, *, user_crs: str) -> None:
        """补充 CRS 后恢复处理。没有可恢复 Job 时不得把版本改成 PROCESSING。"""
        from app.jobs.enums import JobStatus
        from app.jobs.models import Job
        from app.jobs.service import JobService

        locked = self._session.scalar(
            sa.select(AssetVersion).where(AssetVersion.id == version.id).with_for_update()
        )
        if locked is None:
            raise not_found("资产版本", version.id)
        if locked.status is not AssetVersionStatus.NEEDS_INPUT:
            raise conflict(
                code="ASSET_VERSION_NOT_NEEDS_INPUT",
                detail=f"版本 {locked.id} 不处于 NEEDS_INPUT 状态（当前 {locked.status}）",
            )
        job = self._session.scalars(
            sa.select(Job)
            .where(
                Job.asset_version_id == locked.id,
                Job.status == JobStatus.NEEDS_INPUT,
            )
            .order_by(Job.created_at.desc())
            .with_for_update()
        ).first()
        if job is None:
            raise conflict(
                code="NEEDS_INPUT_JOB_MISSING",
                detail=(
                    f"版本 {locked.id} 处于 NEEDS_INPUT，但没有可恢复的任务；"
                    "拒绝将版本改为 PROCESSING"
                ),
            )
        self.upsert_raster_ext(locked.id, user_crs=user_crs)
        self.set_version_status(locked, AssetVersionStatus.PROCESSING)
        JobService(self._session).requeue(job)
