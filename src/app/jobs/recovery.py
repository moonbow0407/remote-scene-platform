"""Job 租约恢复：回收执行者已失联的 RUNNING 任务。

为什么必须有独立恢复器：Worker 崩溃后 RabbitMQ 会重投消息，但该消息可能被
新 Worker 看到时租约未过期而 ACK 掉——此后不再有"下一条消息"，仅靠重投 +
started_at 阈值判定永远无法触发回收。租约过期是唯一可靠的执行权回收信号，
必须由独立的维护循环周期性扫描（属于 Job 基础设施，不与监测计划等业务耦合）。

回收动作（与重试链路统一，全部经 Outbox 投递）：
- attempt 未耗尽 → RETRYING + 生成 job.dispatch 投递事件（同事务）；
- attempt 已耗尽 → FAILED 终态，并同步终止关联资产版本的伪运行状态
  （仅当版本存在且状态机允许，避免拖垮整批恢复）。
"""

import logging
from datetime import timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.context import now_utc
from app.jobs.enums import TASK_BY_JOB_TYPE, JobStatus, OutboxStatus
from app.jobs.models import Job, OutboxEvent
from app.jobs.service import JobService

logger = logging.getLogger(__name__)

# 恢复重投的固定小延迟：执行者刚崩溃时避免与其残留写入竞争
_RECOVERY_REQUEUE_DELAY_SECONDS = 5


def recover_expired_leases(session: Session, *, batch_size: int = 50) -> list[int]:
    """回收一批租约过期的 RUNNING Job；返回本次回收的 Job ID。

    FOR UPDATE SKIP LOCKED：多恢复器实例并发扫描不重叠，也不阻塞正常业务写。
    """
    now = now_utc()
    stmt = (
        sa.select(Job)
        .where(
            Job.status == JobStatus.RUNNING,
            sa.or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < now),
        )
        .order_by(Job.started_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    jobs = list(session.scalars(stmt))
    if not jobs:
        return []

    service = JobService(session)
    recovered: list[int] = []
    for job in jobs:
        detail: dict[str, Any] = {
            "code": "LEASE_LOST",
            "detail": f"执行租约已过期（lease_expires_at={job.lease_expires_at}），"
            "执行者未续约，按失联回收",
            "transient": True,
        }
        if job.attempt >= job.max_attempts:
            service.transition(job, JobStatus.FAILED, event_type="JOB_LEASE_EXPIRED", detail=detail)
            job.last_error = detail
            _fail_related_version_if_possible(session, job)
            logger.error("租约过期且重试次数耗尽，任务失败", extra={"job_id": str(job.id)})
        else:
            service.transition(
                job, JobStatus.RETRYING, event_type="JOB_LEASE_EXPIRED", detail=detail
            )
            job.last_error = detail
            session.add(
                OutboxEvent(
                    aggregate_type="job",
                    aggregate_id=job.id,
                    event_type="job.dispatch",
                    payload={
                        "task": TASK_BY_JOB_TYPE[job.job_type],
                        "args": [str(job.id)],
                        "job_id": str(job.id),
                    },
                    status=OutboxStatus.PENDING,
                    next_attempt_at=now + timedelta(seconds=_RECOVERY_REQUEUE_DELAY_SECONDS),
                )
            )
            session.flush()
            logger.warning("租约过期，任务回收重投", extra={"job_id": str(job.id)})
        recovered.append(job.id)
    return recovered


def _fail_related_version_if_possible(session: Session, job: Job) -> None:
    """重试耗尽时同步终止关联资产版本的伪运行状态；非入库任务或状态不允许时跳过。"""
    owner_kind = job.payload.get("owner_kind") or job.owner_kind
    owner_id = job.payload.get("owner_id") or job.owner_id
    if owner_kind is None or owner_id is None:
        return
    from app.imagery.enums import RecordKind, RecordStatus
    from app.imagery.record_state import is_record_transition_allowed
    from app.imagery.service import ImageryService

    imagery = ImageryService(session)
    row = imagery.get_by_id(RecordKind(str(owner_kind)), int(owner_id))
    if row is None or not is_record_transition_allowed(row.status, RecordStatus.FAILED):
        return
    imagery.set_status(
        row,
        RecordStatus.FAILED,
        diagnostics={
            "reason": "LEASE_LOST_EXHAUSTED",
            "detail": f"任务 {job.id} 执行租约过期且重试次数耗尽",
        },
    )
