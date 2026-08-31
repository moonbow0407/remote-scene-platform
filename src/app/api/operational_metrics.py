"""Stage 6 运维指标采集：PostgreSQL 业务状态为权威，RabbitMQ 只补充队列深度。"""

from __future__ import annotations

import logging
from datetime import timedelta
from urllib.parse import quote

import httpx
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.api.metrics import (
    CLEANUP_BACKLOG,
    JOB_DURATION,
    JOB_FAILURES_24H,
    JOBS_BY_STATUS,
    OPERATIONAL_COLLECTOR_UP,
    OUTBOX_BACKLOG,
    RABBITMQ_QUEUE_DEPTH,
    STORAGE_BYTES,
    WORKER_CONSUMERS,
    WORKER_UTILIZATION,
)
from app.assets.enums import ObjectCleanupStatus
from app.assets.models import DataAsset, ObjectCleanupTask
from app.context import now_utc
from app.db import session_scope
from app.jobs.enums import JobStatus, OutboxStatus
from app.jobs.models import Job, OutboxEvent
from app.settings import Settings

logger = logging.getLogger(__name__)


def refresh_database_metrics(factory: sessionmaker[Session]) -> None:
    """刷新数据库可推导的当前值；失败时保留旧值并把 collector_up 置零。"""
    try:
        with session_scope(factory) as session:
            OUTBOX_BACKLOG.set(
                int(
                    session.scalar(
                        sa.select(sa.func.count())
                        .select_from(OutboxEvent)
                        .where(OutboxEvent.status.in_((OutboxStatus.PENDING, OutboxStatus.CLAIMED)))
                    )
                    or 0
                )
            )
            counts: dict[JobStatus, int] = {
                status: int(count)
                for status, count in session.execute(
                    sa.select(Job.status, sa.func.count()).group_by(Job.status)
                ).tuples()
            }
            for status in JobStatus:
                JOBS_BY_STATUS.labels(status=status.value).set(int(counts.get(status, 0)))
            JOB_FAILURES_24H.set(
                int(
                    session.scalar(
                        sa.select(sa.func.count())
                        .select_from(Job)
                        .where(
                            Job.status == JobStatus.FAILED,
                            Job.finished_at >= now_utc() - timedelta(hours=24),
                        )
                    )
                    or 0
                )
            )
            completed = list(
                session.execute(
                    sa.select(Job.started_at, Job.finished_at)
                    .where(Job.started_at.is_not(None), Job.finished_at.is_not(None))
                    .order_by(Job.finished_at.desc())
                    .limit(1000)
                )
            )
            durations = [
                max(0.0, (finished - started).total_seconds())
                for started, finished in completed
                if started is not None and finished is not None
            ]
            JOB_DURATION.labels(aggregation="average").set(
                sum(durations) / len(durations) if durations else 0
            )
            JOB_DURATION.labels(aggregation="max").set(max(durations, default=0))
            STORAGE_BYTES.labels(kind="original_blob").set(
                int(session.scalar(sa.select(sa.func.sum(DataAsset.size_bytes))) or 0)
            )
            STORAGE_BYTES.labels(kind="derived_artifact").set(0)
            CLEANUP_BACKLOG.set(
                int(
                    session.scalar(
                        sa.select(sa.func.count())
                        .select_from(ObjectCleanupTask)
                        .where(
                            ObjectCleanupTask.status.in_(
                                (
                                    ObjectCleanupStatus.PENDING,
                                    ObjectCleanupStatus.CLAIMED,
                                    ObjectCleanupStatus.RETRYING,
                                )
                            )
                        )
                    )
                    or 0
                )
            )
        OPERATIONAL_COLLECTOR_UP.labels(component="database").set(1)
    except Exception as exc:
        OPERATIONAL_COLLECTOR_UP.labels(component="database").set(0)
        logger.warning("数据库运维指标采集失败", extra={"detail": str(exc)})


async def refresh_rabbitmq_metrics(settings: Settings, factory: sessionmaker[Session]) -> None:
    """读取 RabbitMQ Management API；不可达不影响 Prometheus 抓取本身。"""
    encoded_vhost = quote("/", safe="")
    url = f"{settings.rabbitmq_management_url.rstrip('/')}/api/queues/{encoded_vhost}/geo"
    try:
        async with httpx.AsyncClient(timeout=settings.readiness_timeout_seconds) as client:
            response = await client.get(
                url,
                auth=(settings.rabbitmq_management_user, settings.rabbitmq_management_password),
            )
            response.raise_for_status()
            payload = response.json()
        ready = int(payload.get("messages_ready", 0))
        unacked = int(payload.get("messages_unacknowledged", 0))
        consumers = int(payload.get("consumers", 0))
        RABBITMQ_QUEUE_DEPTH.labels(state="ready").set(ready)
        RABBITMQ_QUEUE_DEPTH.labels(state="unacknowledged").set(unacked)
        WORKER_CONSUMERS.set(consumers)
        with session_scope(factory) as session:
            running = int(
                session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Job)
                    .where(Job.status == JobStatus.RUNNING)
                )
                or 0
            )
        WORKER_UTILIZATION.set(running / consumers if consumers else 0)
        OPERATIONAL_COLLECTOR_UP.labels(component="rabbitmq").set(1)
    except Exception as exc:
        OPERATIONAL_COLLECTOR_UP.labels(component="rabbitmq").set(0)
        logger.warning("RabbitMQ 运维指标采集失败", extra={"detail": str(exc)})
