"""资产生命周期：软删除、恢复、过期清理与 MinIO 删除重试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.assets.enums import AssetStatus, ObjectCleanupKind, ObjectCleanupStatus
from app.assets.models import DataAsset, ObjectCleanupTask
from app.assets.service import AssetService
from app.context import ActorContext, get_actor, now_utc
from app.errors import conflict, not_found
from app.jobs.service import JobService
from app.monitoring.service import MonitoringService
from app.uploads.minio import MinioAdapter

_CLEANUP_RETRY_MAX_SECONDS = 3600


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AssetLifecycleService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def soft_delete(
        self,
        asset_id: int,
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
        asset.deleted_by = None if actor.actor_id is None else int(actor.actor_id)
        asset.purge_attempts = 0
        asset.purge_next_attempt_at = None
        asset.purge_last_error = None
        JobService(self._session).request_cancel_for_assets([asset_id])
        if asset.status in (
            AssetStatus.VALIDATING,
            AssetStatus.PROCESSING,
            AssetStatus.NEEDS_INPUT,
            AssetStatus.UPLOADING,
        ):
            AssetService(self._session).mark_cancelled(asset_id, reason="ASSET_DELETED")
        self._session.flush()
        return asset

    def restore(self, asset_id: int, *, now: datetime | None = None) -> DataAsset:
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

    def due_asset_ids(self, *, now: datetime, limit: int) -> list[int]:
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

    def purge_asset(self, asset_id: int, *, now: datetime | None = None) -> bool:
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
        if JobService(self._session).has_active_for_assets([asset_id]):
            self._defer_asset(asset, "仍有 Worker 持有执行租约，等待取消检查点", ts, 60)
            return False
        keys = [
            key
            for key in (asset.original_object_key, asset.cog_object_key, asset.thumbnail_object_key)
            if key
        ]
        # Job.asset_id 虽 CASCADE，Outbox 无外键：必须先收掉 Job/Outbox，
        # 否则 Dispatcher 仍会把已删除任务投进共享 geo 队列。
        JobService(self._session).delete_jobs_and_outbox_for_assets([asset_id])
        self._session.execute(sa.delete(DataAsset).where(DataAsset.id == asset_id))
        self._session.flush()
        for key in keys:
            self._enqueue_cleanup(object_key=key)
        return True

    def record_purge_failure(self, asset_id: int, detail: str, *, now: datetime) -> None:
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

    def _enqueue_cleanup(self, *, object_key: str) -> ObjectCleanupTask:
        row = self._session.scalar(
            sa.select(ObjectCleanupTask)
            .where(ObjectCleanupTask.object_key == object_key)
            .with_for_update()
        )
        if row is None:
            row = ObjectCleanupTask(kind=ObjectCleanupKind.OBJECT, object_key=object_key)
            self._session.add(row)
        else:
            row.kind = ObjectCleanupKind.OBJECT
            row.status = ObjectCleanupStatus.PENDING
            row.attempts = 0
            row.claimed_at = None
            row.next_attempt_at = None
            row.last_error = None
        self._session.flush()
        return row


class ObjectCleanupService:
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
            self._minio.delete_object(key=task.object_key)
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
