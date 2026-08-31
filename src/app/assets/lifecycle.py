"""Stage 6 资产生命周期：软删除、恢复、过期清理与 MinIO 删除重试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.assets.enums import (
    ArtifactKind,
    AssetVersionStatus,
    ObjectCleanupKind,
    ObjectCleanupStatus,
)
from app.assets.models import (
    AssetArtifact,
    AssetVersion,
    DataAsset,
    ObjectBlob,
    ObjectCleanupTask,
)
from app.assets.service import AssetService
from app.context import ActorContext, get_actor, now_utc
from app.errors import conflict, not_found
from app.ids import new_uuid7
from app.jobs.service import JobService
from app.monitoring.service import MonitoringService
from app.uploads.minio import MinioAdapter

_CLEANUP_RETRY_MAX_SECONDS = 3600


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AssetLifecycleService:
    """资产生命周期用例；事务边界由 API 或 cleanup 进程持有。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def soft_delete(
        self,
        asset_id: UUID,
        *,
        retention_days: int,
        actor: ActorContext | None = None,
        now: datetime | None = None,
    ) -> DataAsset:
        asset = self._session.scalar(
            sa.select(DataAsset).where(DataAsset.id == asset_id).with_for_update()
        )
        if asset is None:
            raise not_found("资产", asset_id)
        if asset.deleted_at is not None:
            return asset
        ts = now or now_utc()
        actor = actor or get_actor()
        asset.deleted_at = ts
        asset.purge_after = ts + timedelta(days=retention_days)
        asset.deleted_by = None if actor.actor_id is None else UUID(actor.actor_id)
        asset.purge_attempts = 0
        asset.purge_next_attempt_at = None
        asset.purge_last_error = None

        versions = list(
            self._session.scalars(sa.select(AssetVersion).where(AssetVersion.asset_id == asset_id))
        )
        JobService(self._session).request_cancel_for_versions([row.id for row in versions])
        # 被取消的非终态版本不能在恢复后伪装为仍在处理；READY/FAILED 历史保持不变。
        for version in versions:
            if version.status in (
                AssetVersionStatus.VALIDATING,
                AssetVersionStatus.PROCESSING,
                AssetVersionStatus.NEEDS_INPUT,
            ):
                AssetService(self._session).mark_version_cancelled(
                    version.id, reason="ASSET_DELETED"
                )
        self._session.flush()
        return asset

    def restore(self, asset_id: UUID, *, now: datetime | None = None) -> DataAsset:
        asset = self._session.scalar(
            sa.select(DataAsset).where(DataAsset.id == asset_id).with_for_update()
        )
        if asset is None:
            raise not_found("资产", asset_id)
        if asset.deleted_at is None:
            return asset
        ts = now or now_utc()
        assert asset.purge_after is not None
        if _as_utc(asset.purge_after) <= _as_utc(ts):
            raise conflict(
                code="ASSET_RESTORE_WINDOW_EXPIRED",
                detail=f"资产 {asset_id} 的 7 天恢复期已结束，不能恢复",
            )
        asset.deleted_at = None
        asset.purge_after = None
        asset.deleted_by = None
        asset.purge_attempts = 0
        asset.purge_next_attempt_at = None
        asset.purge_last_error = None
        self._session.flush()
        return asset

    def due_asset_ids(self, *, now: datetime, limit: int) -> list[UUID]:
        return list(
            self._session.scalars(
                sa.select(DataAsset.id)
                .where(
                    DataAsset.deleted_at.is_not(None),
                    DataAsset.purge_after <= now,
                    sa.or_(
                        DataAsset.purge_next_attempt_at.is_(None),
                        DataAsset.purge_next_attempt_at <= now,
                    ),
                )
                .order_by(DataAsset.purge_after)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )

    def purge_asset(self, asset_id: UUID, *, now: datetime | None = None) -> bool:
        """物理清理一个过期资产；仍被监测快照引用时延后而非破坏审计证据。"""
        ts = now or now_utc()
        asset = self._session.scalar(
            sa.select(DataAsset).where(DataAsset.id == asset_id).with_for_update()
        )
        if asset is None:
            return False
        if asset.deleted_at is None or asset.purge_after is None:
            return False
        if _as_utc(asset.purge_after) > _as_utc(ts):
            return False
        if MonitoringService(self._session).asset_has_snapshot_references(asset_id):
            self._defer_asset(asset, "资产仍被监测输入快照引用，保留历史证据", ts, 86400)
            return False

        versions = list(
            self._session.scalars(sa.select(AssetVersion).where(AssetVersion.asset_id == asset_id))
        )
        version_ids = [row.id for row in versions]
        if JobService(self._session).has_active_for_versions(version_ids):
            self._defer_asset(asset, "仍有 Worker 持有执行租约，等待取消检查点", ts, 60)
            return False
        artifacts = (
            list(
                self._session.scalars(
                    sa.select(AssetArtifact).where(AssetArtifact.asset_version_id.in_(version_ids))
                )
            )
            if version_ids
            else []
        )
        blob_ids = {row.blob_id for row in versions if row.blob_id is not None}

        # 使用数据库级级联删除，不让 ORM 把不可空 asset_id 尝试更新为 NULL。
        self._session.execute(sa.delete(DataAsset).where(DataAsset.id == asset_id))
        self._session.flush()

        for artifact in artifacts:
            if artifact.kind is not ArtifactKind.ORIGINAL:
                self._enqueue_cleanup(
                    kind=ObjectCleanupKind.ARTIFACT,
                    object_key=artifact.object_key,
                    blob_id=None,
                )
        for blob_id in blob_ids:
            blob = self._session.scalar(
                sa.select(ObjectBlob).where(ObjectBlob.id == blob_id).with_for_update()
            )
            if blob is None:
                continue
            live_references = int(
                self._session.scalar(
                    sa.select(sa.func.count())
                    .select_from(AssetVersion)
                    .where(AssetVersion.blob_id == blob_id)
                )
                or 0
            )
            blob.reference_count = live_references
            if live_references == 0:
                self._enqueue_cleanup(
                    kind=ObjectCleanupKind.BLOB,
                    object_key=blob.object_key,
                    blob_id=blob.id,
                )
        return True

    def record_purge_failure(self, asset_id: UUID, detail: str, *, now: datetime) -> None:
        asset = self._session.get(DataAsset, asset_id)
        if asset is None:
            return
        asset.purge_attempts += 1
        delay = min(2 ** min(asset.purge_attempts, 12), _CLEANUP_RETRY_MAX_SECONDS)
        asset.purge_next_attempt_at = now + timedelta(seconds=delay)
        asset.purge_last_error = detail[:4000]

    def _defer_asset(
        self, asset: DataAsset, detail: str, now: datetime, delay_seconds: int
    ) -> None:
        asset.purge_attempts += 1
        asset.purge_next_attempt_at = now + timedelta(seconds=delay_seconds)
        asset.purge_last_error = detail

    def _enqueue_cleanup(
        self, *, kind: ObjectCleanupKind, object_key: str, blob_id: UUID | None
    ) -> ObjectCleanupTask:
        row = self._session.scalar(
            sa.select(ObjectCleanupTask)
            .where(ObjectCleanupTask.object_key == object_key)
            .with_for_update()
        )
        if row is None:
            row = ObjectCleanupTask(
                id=new_uuid7(), kind=kind, object_key=object_key, blob_id=blob_id
            )
            self._session.add(row)
        else:
            row.kind = kind
            row.blob_id = blob_id
            row.status = ObjectCleanupStatus.PENDING
            row.attempts = 0
            row.claimed_at = None
            row.next_attempt_at = None
            row.last_error = None
        self._session.flush()
        return row


class ObjectCleanupService:
    """幂等执行对象删除；BLOB 删除前在行锁下重新核对真实引用。"""

    def __init__(self, session: Session, minio: MinioAdapter) -> None:
        self._session = session
        self._minio = minio

    def claim_due(self, *, now: datetime, limit: int) -> list[ObjectCleanupTask]:
        rows = list(
            self._session.scalars(
                sa.select(ObjectCleanupTask)
                .where(
                    ObjectCleanupTask.status.in_(
                        (ObjectCleanupStatus.PENDING, ObjectCleanupStatus.RETRYING)
                    ),
                    sa.or_(
                        ObjectCleanupTask.next_attempt_at.is_(None),
                        ObjectCleanupTask.next_attempt_at <= now,
                    ),
                )
                .order_by(ObjectCleanupTask.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            row.status = ObjectCleanupStatus.CLAIMED
            row.claimed_at = now
        return rows

    def execute(self, task: ObjectCleanupTask, *, now: datetime) -> bool:
        try:
            if task.kind is ObjectCleanupKind.BLOB and task.blob_id is not None:
                blob = self._session.scalar(
                    sa.select(ObjectBlob).where(ObjectBlob.id == task.blob_id).with_for_update()
                )
                if blob is not None:
                    actual = int(
                        self._session.scalar(
                            sa.select(sa.func.count())
                            .select_from(AssetVersion)
                            .where(AssetVersion.blob_id == blob.id)
                        )
                        or 0
                    )
                    blob.reference_count = actual
                    if actual > 0:
                        task.status = ObjectCleanupStatus.SUCCEEDED
                        task.last_error = "对象已重新被 live 版本引用，取消物理删除"
                        return False
            self._minio.delete_object(key=task.object_key)
            if task.kind is ObjectCleanupKind.BLOB and task.blob_id is not None:
                blob = self._session.get(ObjectBlob, task.blob_id)
                if blob is not None and blob.reference_count == 0:
                    self._session.delete(blob)
            task.status = ObjectCleanupStatus.SUCCEEDED
            task.next_attempt_at = None
            task.last_error = None
            return True
        except Exception as exc:
            task.attempts += 1
            task.status = ObjectCleanupStatus.RETRYING
            delay = min(2 ** min(task.attempts, 12), _CLEANUP_RETRY_MAX_SECONDS)
            task.next_attempt_at = now + timedelta(seconds=delay)
            task.last_error = str(exc)[:4000]
            return False
