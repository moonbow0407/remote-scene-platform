"""影像硬删除：立刻删行，MinIO 对象经 object_cleanup_task 退避删除。"""

from __future__ import annotations

from datetime import datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.errors import not_found
from app.imagery.enums import ObjectCleanupKind, ObjectCleanupStatus, RecordKind
from app.imagery.models import ObjectCleanupTask
from app.imagery.types import RECORD_LABEL, record_cls
from app.jobs.service import JobService
from app.uploads.minio import MinioAdapter
from app.uploads.models import UploadSession

_CLEANUP_RETRY_MAX_SECONDS = 3600


class ImageryLifecycleService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def delete_record(self, kind: RecordKind, record_id: int) -> None:
        model = record_cls(kind)
        row = self._session.scalar(sa.select(model).where(model.id == record_id).with_for_update())
        if row is None:
            raise not_found(RECORD_LABEL[kind], record_id)
        keys = [key for key in (row.original_object_key, row.cog_object_key) if key]
        jobs = JobService(self._session)
        jobs.request_cancel_for_owner(kind.value, record_id)
        jobs.delete_jobs_and_outbox_for_owner(kind.value, record_id)
        self._session.execute(
            sa.delete(UploadSession).where(
                UploadSession.owner_kind == kind.value, UploadSession.owner_id == record_id
            )
        )
        self._session.execute(sa.delete(model).where(model.id == record_id))
        self._session.flush()
        for key in keys:
            self._enqueue_cleanup(object_key=key)

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
