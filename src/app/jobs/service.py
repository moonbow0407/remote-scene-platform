"""jobs 服务：Job 生命周期与 Outbox 可靠投递的领域入口。

边界说明：
- create_job_with_outbox：与业务写入（如资产版本创建）共用同一 Session，
  由调用方保证同事务提交；
- transition：唯一合法的状态修改入口（状态机校验 + 事件追加）；
- Outbox 认领/发布状态更新仅由 Dispatcher 进程调用。
"""

import logging
from datetime import timedelta
from typing import Any, NamedTuple
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.context import now_utc
from app.ids import new_uuid7
from app.jobs.enums import TASK_BY_JOB_TYPE, JobStatus, JobType, OutboxStatus
from app.jobs.models import Job, JobEvent, OutboxEvent
from app.jobs.state_machine import is_transition_allowed

logger = logging.getLogger(__name__)

# RUNNING 超过该时长且消息被重投，视为 Worker 崩溃留下的陈旧认领
_STALE_RUNNING_SECONDS = 30 * 60


class JobClaim(NamedTuple):
    """Worker 认领结果。acquired=True 表示本调用取得执行权，必须且仅此 Worker 执行。"""

    job: Job
    acquired: bool


class JobService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_job_with_outbox(
        self,
        *,
        job_type: JobType,
        asset_version_id: UUID,
        payload: dict[str, Any],
        max_attempts: int = 4,
    ) -> tuple[Job, OutboxEvent]:
        """创建 Job 并同事务生成 Outbox 事件（由调用方的事务提交）。

        幂等语义：Job 创建与投递解耦；重复调用会创建重复 Job，
        调用方（上传完成）以唯一会话状态保证只调用一次。
        """
        job_id = new_uuid7()
        job = Job(
            id=job_id,
            job_type=job_type,
            status=JobStatus.PENDING,
            payload={**payload, "job_id": str(job_id)},
            max_attempts=max_attempts,
            asset_version_id=asset_version_id,
        )
        self._session.add(job)
        self._session.flush()

        event = OutboxEvent(
            id=new_uuid7(),
            aggregate_type="job",
            aggregate_id=job_id,
            event_type="job.dispatch",
            payload={
                "task": TASK_BY_JOB_TYPE[job_type],
                "args": [str(job_id)],
                "job_id": str(job_id),
            },
            status=OutboxStatus.PENDING,
        )
        self._session.add(event)
        self._session.flush()
        self.append_event(job_id, event_type="JOB_CREATED", detail={"job_type": job_type.value})
        return job, event

    def get(self, job_id: UUID) -> Job | None:
        return self._session.get(Job, job_id)

    def get_required(self, job_id: UUID) -> Job:
        job = self.get(job_id)
        if job is None:
            from app.errors import not_found

            raise not_found("任务", job_id)
        return job

    def transition(
        self,
        job: Job,
        target: JobStatus,
        *,
        detail: dict[str, Any] | None = None,
        event_type: str = "STATUS_CHANGED",
        set_timestamps: bool = True,
    ) -> Job:
        """唯一合法的状态转换入口：校验状态机并追加事件。"""
        current = job.status
        if not is_transition_allowed(current, target):
            from app.errors import conflict

            raise conflict(
                code="JOB_STATE_TRANSITION_INVALID",
                detail=f"任务 {job.id} 不允许从 {current} 转换到 {target}",
            )
        job.status = target
        if set_timestamps:
            ts = now_utc()
            if target is JobStatus.QUEUED and job.queued_at is None:
                job.queued_at = ts
            if target is JobStatus.RUNNING:
                job.started_at = ts
            if target in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED):
                job.finished_at = ts
        self.append_event(
            job.id, event_type=event_type, from_status=current, to_status=target, detail=detail
        )
        return job

    def append_event(
        self,
        job_id: UUID,
        *,
        event_type: str,
        from_status: JobStatus | None = None,
        to_status: JobStatus | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._session.add(
            JobEvent(
                id=new_uuid7(),
                job_id=job_id,
                event_type=event_type,
                from_status=from_status,
                to_status=to_status,
                detail=detail,
            )
        )

    def requeue(self, job: Job) -> OutboxEvent:
        """把 NEEDS_INPUT 等待中的任务重新入队：生成新投递事件（同一事务）。"""
        event = OutboxEvent(
            id=new_uuid7(),
            aggregate_type="job",
            aggregate_id=job.id,
            event_type="job.dispatch",
            payload={
                "task": TASK_BY_JOB_TYPE[job.job_type],
                "args": [str(job.id)],
                "job_id": str(job.id),
            },
            status=OutboxStatus.PENDING,
        )
        self._session.add(event)
        self._session.flush()
        self.transition(job, JobStatus.QUEUED, event_type="JOB_REQUEUED")
        return event

    def claim_for_run(self, job_id: UUID) -> JobClaim:
        """Worker 认领：QUEUED/RETRYING/PENDING → RUNNING。

        行锁保证同一 Job 同一时刻只有一个调用者 acquired=True。
        重复投递看到 RUNNING（未过期）或终态时 acquired=False，调用方必须跳过执行。
        RUNNING 超过陈旧阈值（Worker 崩溃后消息重投）时回收再认领。
        """
        job = self._session.scalar(sa.select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            from app.errors import not_found

            raise not_found("任务", job_id)
        now = now_utc()
        if (
            job.status is JobStatus.RUNNING
            and job.started_at is not None
            and (now - job.started_at).total_seconds() > _STALE_RUNNING_SECONDS
        ):
            logger.warning("任务 RUNNING 超时，按陈旧认领回收重跑", extra={"job_id": str(job.id)})
            self.transition(job, JobStatus.RETRYING, event_type="JOB_STALE_RECLAIMED")
        if job.status in (JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RETRYING):
            job.attempt = job.attempt + 1
            self.transition(job, JobStatus.RUNNING, event_type="JOB_CLAIMED")
            return JobClaim(job=job, acquired=True)
        return JobClaim(job=job, acquired=False)


class OutboxRepository:
    """Outbox 认领与状态回写；仅 Dispatcher 进程使用。"""

    def __init__(self, session: Session) -> None:
        self._session = session

    def claim_batch(self, *, batch_size: int, claim_ttl_seconds: int) -> list[OutboxEvent]:
        """认领一批待投递事件。

        FOR UPDATE SKIP LOCKED 保证多 Dispatcher 并发认领不重叠；
        已过期 CLAIMED（陈旧认领）会被再次认领，实现断点恢复。
        """
        now = now_utc()
        stmt = (
            sa.select(OutboxEvent)
            .where(
                sa.or_(
                    OutboxEvent.status == OutboxStatus.PENDING,
                    sa.and_(
                        OutboxEvent.status == OutboxStatus.CLAIMED,
                        OutboxEvent.claim_expires_at < now,
                    ),
                )
            )
            .where(
                sa.or_(
                    OutboxEvent.next_attempt_at.is_(None),
                    OutboxEvent.next_attempt_at <= now,
                )
            )
            .order_by(OutboxEvent.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        rows = list(self._session.scalars(stmt))
        for row in rows:
            row.status = OutboxStatus.CLAIMED
            row.claimed_at = now
            row.claim_expires_at = now + timedelta(seconds=claim_ttl_seconds)
        return rows

    def mark_published(self, event: OutboxEvent) -> None:
        event.status = OutboxStatus.PUBLISHED
        event.published_at = now_utc()
        event.claim_expires_at = None

    def mark_retry(self, event: OutboxEvent) -> None:
        """发布失败回退：保持 PENDING 并按指数退避（上限 60s），等待下一轮认领。"""
        backoff = min(2 ** int(event.attempts), 60)
        event.status = OutboxStatus.PENDING
        event.attempts = event.attempts + 1
        event.next_attempt_at = now_utc() + timedelta(seconds=backoff)
        event.claim_expires_at = None

    def set_job_queued_after_publish(self, job_ids: list[UUID]) -> None:
        """发布成功后把仍处于 PENDING 的 Job 推进到 QUEUED（尽力而为）。"""
        if not job_ids:
            return
        rows = list(
            self._session.scalars(
                sa.select(Job).where(Job.id.in_(job_ids), Job.status == JobStatus.PENDING)
            )
        )
        service = JobService(self._session)
        for job in rows:
            service.transition(job, JobStatus.QUEUED, event_type="JOB_DISPATCHED")
