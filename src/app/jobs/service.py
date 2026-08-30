"""jobs 服务：Job 生命周期与 Outbox 可靠投递的领域入口。

边界说明：
- create_job_with_outbox：与业务写入（如资产版本创建）共用同一 Session，
  由调用方保证同事务提交；
- transition：唯一合法的状态修改入口（状态机校验 + 事件追加）；
- Outbox 认领/发布状态更新仅由 Dispatcher 进程调用。
"""

import logging
from datetime import datetime, timedelta
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

# 瞬时错误重试的指数退避：5s → 10s → 20s … 上限 300s（由 Outbox next_attempt_at 承载）
_RETRY_BACKOFF_BASE_SECONDS = 5
_RETRY_BACKOFF_MAX_SECONDS = 300
# 默认租约 TTL；须明显大于心跳间隔（Settings 校验两者关系）
_DEFAULT_LEASE_TTL_SECONDS = 600


def retry_backoff_seconds(attempt: int) -> int:
    return min(_RETRY_BACKOFF_BASE_SECONDS * 2 ** max(0, attempt - 1), _RETRY_BACKOFF_MAX_SECONDS)


class JobClaim(NamedTuple):
    """Worker 认领结果。acquired=True 表示本调用取得执行权（含租约），必须且仅此 Worker 执行。"""

    job: Job
    acquired: bool
    lease_token: UUID | None


class JobService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_job_with_outbox(
        self,
        *,
        job_type: JobType,
        payload: dict[str, Any],
        asset_version_id: UUID | None = None,
        max_attempts: int = 4,
    ) -> tuple[Job, OutboxEvent]:
        """创建 Job 并同事务生成 Outbox 事件（由调用方的事务提交）。

        幂等语义：Job 创建与投递解耦；重复调用会创建重复 Job，
        调用方（上传完成）以唯一会话状态保证只调用一次。
        asset_version_id 仅入库任务必填；MONITORING_RUN 为多版本输入快照，
        权威关联在 monitoring_run_input，不伪造单版本引用。
        """
        if asset_version_id is None and job_type is not JobType.MONITORING_RUN:
            raise ValueError(
                f"任务类型 {job_type.value} 必须引用具体 asset_version_id；"
                "仅 MONITORING_RUN 允许无单版本引用"
            )
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

        event = self._dispatch_event(job)
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

    def request_cancel(self, job: Job) -> Job:
        """请求取消任务；未开始的任务立即终止，运行中任务由步骤检查点收敛。"""
        if job.status in (
            JobStatus.PENDING,
            JobStatus.QUEUED,
            JobStatus.RETRYING,
            JobStatus.NEEDS_INPUT,
        ):
            return self.transition(job, JobStatus.CANCELLED, event_type="JOB_CANCELLED")
        if job.status is JobStatus.RUNNING:
            return self.transition(
                job, JobStatus.CANCEL_REQUESTED, event_type="JOB_CANCEL_REQUESTED"
            )
        return job

    def cancellation_checkpoint(self, job_id: UUID) -> bool:
        """Worker 步骤边界检查取消标志；返回 True 时调用方必须立即停止。"""
        job = self._session.scalar(sa.select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            return True
        if job.status is JobStatus.CANCEL_REQUESTED:
            self.transition(job, JobStatus.CANCELLED, event_type="JOB_CANCELLED_AT_CHECKPOINT")
            return True
        return job.status is JobStatus.CANCELLED

    def request_cancel_for_versions(self, version_ids: list[UUID]) -> list[UUID]:
        """资产软删除时取消关联的非终态入库任务。"""
        if not version_ids:
            return []
        jobs = list(
            self._session.scalars(
                sa.select(Job).where(
                    Job.asset_version_id.in_(version_ids),
                    Job.status.in_(
                        (
                            JobStatus.PENDING,
                            JobStatus.QUEUED,
                            JobStatus.RUNNING,
                            JobStatus.RETRYING,
                            JobStatus.NEEDS_INPUT,
                            JobStatus.CANCEL_REQUESTED,
                        )
                    ),
                )
            )
        )
        for job in jobs:
            self.request_cancel(job)
        return [job.id for job in jobs]

    def has_active_for_versions(self, version_ids: list[UUID]) -> bool:
        if not version_ids:
            return False
        return bool(
            self._session.scalar(
                sa.select(sa.func.count())
                .select_from(Job)
                .where(
                    Job.asset_version_id.in_(version_ids),
                    Job.status.in_((JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED)),
                )
            )
        )

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

    def _dispatch_event(self, job: Job, *, delay_seconds: int | None = None) -> OutboxEvent:
        """构造本 Job 的投递事件（调用方事务内提交）；delay_seconds 用于重试退避。"""
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
            next_attempt_at=(
                now_utc() + timedelta(seconds=delay_seconds) if delay_seconds else None
            ),
        )
        self._session.add(event)
        self._session.flush()
        return event

    def requeue(self, job: Job) -> OutboxEvent:
        """把 NEEDS_INPUT 等待中的任务重新入队：生成新投递事件（同一事务）。"""
        event = self._dispatch_event(job)
        self.transition(job, JobStatus.QUEUED, event_type="JOB_REQUEUED")
        return event

    def schedule_retry(self, job: Job, *, detail: dict[str, Any]) -> OutboxEvent | None:
        """瞬时错误重试：RETRYING 与重投事件同事务落库，投递完全交给 Dispatcher。

        不能再用 Celery self.retry() 负责重投：PostgreSQL 写 RETRYING 与 Broker
        发布是两次独立写入，发布失败会留下"RETRYING 但永远没有消息"的死窗口；
        Outbox 保证两者原子。返回 None 表示重试次数已耗尽，Job 已置 FAILED，
        调用方必须同步落关联对象的终态。
        """
        if job.attempt >= job.max_attempts:
            exhausted: dict[str, Any] = {**detail, "code": "TRANSIENT_EXHAUSTED"}
            self.transition(job, JobStatus.FAILED, event_type="JOB_FAILED", detail=exhausted)
            job.last_error = {
                "code": "TRANSIENT_EXHAUSTED",
                "detail": f"瞬时错误重试次数耗尽：{detail.get('detail', '')}",
                "transient": True,
            }
            return None
        self.transition(job, JobStatus.RETRYING, event_type="JOB_RETRYING", detail=detail)
        job.last_error = dict(detail)
        # 退避由 Outbox next_attempt_at 承载，Dispatcher 到点才认领发布
        return self._dispatch_event(job, delay_seconds=retry_backoff_seconds(job.attempt))

    def claim_for_run(
        self, job_id: UUID, *, lease_ttl_seconds: int = _DEFAULT_LEASE_TTL_SECONDS
    ) -> JobClaim:
        """Worker 认领：QUEUED/RETRYING/PENDING → RUNNING，并取得执行租约。

        行锁保证同一 Job 同一时刻只有一个调用者 acquired=True。
        执行权以租约为准：RUNNING 且租约未过期 → 重复投递，跳过；
        RUNNING 但租约缺失或已过期 → 执行者已失联，回收后重新认领
        （Worker 崩溃后消息可能已被 ACK，恢复由独立恢复器按租约过期兜底）。
        """
        job = self._session.scalar(sa.select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            from app.errors import not_found

            raise not_found("任务", job_id)
        now = now_utc()
        if job.status is JobStatus.RUNNING and not self._lease_valid(job, now):
            logger.warning(
                "任务 RUNNING 但执行租约已失效，按陈旧认领回收重跑", extra={"job_id": str(job.id)}
            )
            self.transition(job, JobStatus.RETRYING, event_type="JOB_STALE_RECLAIMED")
        if job.status in (JobStatus.PENDING, JobStatus.QUEUED, JobStatus.RETRYING):
            token = new_uuid7()
            job.lease_token = token
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=lease_ttl_seconds)
            job.attempt = job.attempt + 1
            self.transition(job, JobStatus.RUNNING, event_type="JOB_CLAIMED")
            return JobClaim(job=job, acquired=True, lease_token=token)
        return JobClaim(job=job, acquired=False, lease_token=None)

    def _lease_valid(self, job: Job, now: datetime) -> bool:
        """租约是否仍被有效持有（NULL/过期一律视为失联）。

        比较在 SQL 侧完成：PostgreSQL timestamptz 读回带时区而 SQLite 读回 naive，
        Python 侧直接比较会因方言差异报错，SQL 侧绑定行为是统一的。
        """
        return bool(
            self._session.scalar(sa.select(Job.lease_expires_at > now).where(Job.id == job.id))
        )


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
