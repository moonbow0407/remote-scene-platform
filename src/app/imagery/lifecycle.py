"""影像生命周期：软删除、恢复、过期清理与 MinIO 删除重试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.context import ActorContext, get_actor, now_utc
from app.errors import conflict, not_found
from app.imagery.enums import ObjectCleanupKind, ObjectCleanupStatus, RecordKind, RecordStatus
from app.imagery.models import ObjectCleanupTask
from app.imagery.service import ImageryService
from app.imagery.types import RECORD_LABEL, RasterRecord, record_cls
from app.jobs.service import JobService
from app.monitoring.service import MonitoringService
from app.uploads.minio import MinioAdapter
from app.uploads.models import UploadSession

_CLEANUP_RETRY_MAX_SECONDS = 3600


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class ImageryLifecycleService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def soft_delete(
        self,
        kind: RecordKind,
        record_id: int,
        *,
        retention_days: int,
        actor: ActorContext | None = None,
        now: datetime | None = None,
    ) -> RasterRecord:
        model = record_cls(kind)
        row = self._session.scalar(sa.select(model).where(model.id == record_id).with_for_update())
        if row is None:
            raise not_found(RECORD_LABEL[kind], record_id)
        if row.deleted_at is not None:
            return row
        ts = now or now_utc()
        actor = actor or get_actor()
        row.deleted_at = ts
        row.purge_after = ts + timedelta(days=retention_days)
        row.deleted_by = None if actor.actor_id is None else int(actor.actor_id)
        row.purge_attempts = 0
        row.purge_next_attempt_at = None
        row.purge_last_error = None
        JobService(self._session).request_cancel_for_owner(kind.value, record_id)
        if row.status in (
            RecordStatus.VALIDATING,
            RecordStatus.PROCESSING,
            RecordStatus.NEEDS_INPUT,
            RecordStatus.UPLOADING,
        ):
            ImageryService(self._session).mark_cancelled(
                kind, record_id, reason="RECORD_DELETED"
            )
        self._session.flush()
        return row

    def restore(
        self, kind: RecordKind, record_id: int, *, now: datetime | None = None
    ) -> RasterRecord:
        model = record_cls(kind)
        row = self._session.scalar(sa.select(model).where(model.id == record_id).with_for_update())
        if row is None:
            raise not_found(RECORD_LABEL[kind], record_id)
        if row.deleted_at is None:
            return row
        ts = now or now_utc()
        assert row.purge_after is not None
        if _as_utc(row.purge_after) <= _as_utc(ts):
            raise conflict(
                code="RECORD_RESTORE_WINDOW_EXPIRED",
                detail=f"{RECORD_LABEL[kind]} {record_id} 的 7 天恢复期已结束，不能恢复",
            )
        row.deleted_at = None
        row.purge_after = None
        row.deleted_by = None
        row.purge_attempts = 0
        row.purge_next_attempt_at = None
        row.purge_last_error = None
        self._session.flush()
        return row

    def due_records(self, *, now: datetime, limit: int) -> list[tuple[RecordKind, int]]:
        found: list[tuple[RecordKind, int]] = []
        remaining = limit
        for kind in (RecordKind.SATELLITE, RecordKind.UAV):
            if remaining <= 0:
                break
            model = record_cls(kind)
            ids = list(
                self._session.scalars(
                    sa.select(model.id)
                    .where(
                        model.deleted_at.is_not(None),
                        model.purge_after <= now,
                        sa.or_(
                            model.purge_next_attempt_at.is_(None),
                            model.purge_next_attempt_at <= now,
                        ),
                    )
                    .order_by(model.purge_after)
                    .limit(remaining)
                    .with_for_update(skip_locked=True)
                )
            )
            found.extend((kind, item_id) for item_id in ids)
            remaining = limit - len(found)
        return found

    def purge_record(
        self, kind: RecordKind, record_id: int, *, now: datetime | None = None
    ) -> bool:
        ts = now or now_utc()
        model = record_cls(kind)
        row = self._session.scalar(sa.select(model).where(model.id == record_id).with_for_update())
        if row is None:
            return False
        if row.deleted_at is None or row.purge_after is None:
            return False
        if _as_utc(row.purge_after) > _as_utc(ts):
            return False
        if MonitoringService(self._session).record_has_snapshot_references(kind.value, record_id):
            self._defer(row, "记录仍被监测输入快照引用，保留历史证据", ts, 86400)
            return False
        if JobService(self._session).has_active_for_owner(kind.value, record_id):
            self._defer(row, "仍有 Worker 持有执行租约，等待取消检查点", ts, 60)
            return False
        keys = [
            key
            for key in (row.original_object_key, row.cog_object_key, row.thumbnail_object_key)
            if key
        ]
        JobService(self._session).delete_jobs_and_outbox_for_owner(kind.value, record_id)
        self._session.execute(
            sa.delete(UploadSession).where(
                UploadSession.owner_kind == kind.value, UploadSession.owner_id == record_id
            )
        )
        self._session.execute(sa.delete(model).where(model.id == record_id))
        self._session.flush()
        for key in keys:
            self._enqueue_cleanup(object_key=key)
        return True

    def record_purge_failure(
        self, kind: RecordKind, record_id: int, detail: str, *, now: datetime
    ) -> None:
        row = self._session.get(record_cls(kind), record_id)
        if row is None:
            return
        row.purge_attempts += 1
        delay = min(2 ** min(row.purge_attempts, 12), _CLEANUP_RETRY_MAX_SECONDS)
        row.purge_next_attempt_at = now + timedelta(seconds=delay)
        row.purge_last_error = detail[:4000]

    def _defer(self, row: RasterRecord, detail: str, now: datetime, delay_seconds: int) -> None:
        row.purge_attempts += 1
        row.purge_next_attempt_at = now + timedelta(seconds=delay_seconds)
        row.purge_last_error = detail

    def _enqueue_cleanup(self, *, object_key: str) -> ObjectCleanupTask:
        existing = self._session.scalar(
            sa.select(ObjectCleanupTask)
            .where(ObjectCleanupTask.object_key == object_key)
            .with_for_update()
        )
        if existing is None:
            existing = ObjectCleanupTask(kind=ObjectCleanupKind.OBJECT, object_key=object_key)
            self._session.add(existing)
        else:
            existing.kind = ObjectCleanupKind.OBJECT
            existing.status = ObjectCleanupStatus.PENDING
            existing.attempts = 0
            existing.claimed_at = None
            existing.next_attempt_at = None
            existing.last_error = None
        self._session.flush()
        return existing


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
