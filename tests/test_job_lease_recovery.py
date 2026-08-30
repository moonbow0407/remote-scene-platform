"""Job 执行租约与恢复器回归。

覆盖三个可靠性接缝：
- 认领即取得租约；重复投递在租约有效期内被跳过；
- 租约过期（执行者失联）由恢复器回收重投——不依赖 Broker 重投消息恰好到达；
- 瞬时重试与状态转换同事务写 Outbox，不再依赖 Celery self.retry 双写。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.context import now_utc
from app.db import Base, session_scope
from app.jobs.enums import JobStatus, JobType, OutboxStatus
from app.jobs.heartbeat import LeaseHeartbeat
from app.jobs.models import Job, JobEvent, OutboxEvent
from app.jobs.recovery import recover_expired_leases
from app.jobs.service import JobService


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(_type: JSONB, compiler: object, **_kw: object) -> str:
    return "JSON"


_TABLES = ("asset_version", "job", "job_event", "outbox_event")


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in _TABLES])
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _aware(value: datetime) -> datetime:
    # SQLite 读回 naive datetime；PostgreSQL 为 timestamptz，读回带时区
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _add_job(factory: sessionmaker[Session], *, max_attempts: int = 4) -> UUID:
    job_id = uuid4()
    with session_scope(factory) as session:
        session.add(
            Job(
                id=job_id,
                job_type=JobType.RASTER_INGESTION,
                status=JobStatus.PENDING,
                payload={"asset_version_id": str(uuid4())},
                asset_version_id=uuid4(),
                max_attempts=max_attempts,
            )
        )
    return job_id


def _update_job(factory: sessionmaker[Session], job_id: UUID, **fields: Any) -> None:
    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        for key, value in fields.items():
            setattr(job, key, value)


def test_claim_grants_lease_and_increments_attempt(factory: sessionmaker[Session]) -> None:
    job_id = _add_job(factory)
    with session_scope(factory) as session:
        claim = JobService(session).claim_for_run(job_id, lease_ttl_seconds=600)
        assert claim.acquired
        assert claim.lease_token is not None
        assert claim.job.status is JobStatus.RUNNING
        assert claim.job.attempt == 1
        assert claim.job.heartbeat_at is not None
        assert claim.job.lease_expires_at is not None
        assert _aware(claim.job.lease_expires_at) > now_utc() + timedelta(seconds=590)


def test_duplicate_delivery_with_valid_lease_is_skipped(factory: sessionmaker[Session]) -> None:
    job_id = _add_job(factory)
    with session_scope(factory) as session:
        first = JobService(session).claim_for_run(job_id)
        assert first.acquired
    with session_scope(factory) as session:
        second = JobService(session).claim_for_run(job_id)
        assert not second.acquired
        assert second.job.attempt == 1
        assert second.lease_token is None
    with session_scope(factory) as session:
        claimed = session.scalar(
            sa.select(sa.func.count())
            .select_from(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.event_type == "JOB_CLAIMED")
        )
        assert claimed == 1


def test_expired_lease_reclaimed_on_redelivery(factory: sessionmaker[Session]) -> None:
    """消息重投但租约已过期：回收后重新认领，取得新 token。"""
    job_id = _add_job(factory)
    with session_scope(factory) as session:
        first = JobService(session).claim_for_run(job_id)
        stale_token = first.lease_token
    _update_job(factory, job_id, lease_expires_at=now_utc() - timedelta(seconds=1))
    with session_scope(factory) as session:
        claim = JobService(session).claim_for_run(job_id)
        assert claim.acquired
        assert claim.lease_token is not None
        assert claim.lease_token != stale_token
        assert claim.job.status is JobStatus.RUNNING
        assert claim.job.attempt == 2
    with session_scope(factory) as session:
        reclaimed = session.scalar(
            sa.select(sa.func.count())
            .select_from(JobEvent)
            .where(JobEvent.job_id == job_id, JobEvent.event_type == "JOB_STALE_RECLAIMED")
        )
        assert reclaimed == 1


def test_recovery_requeues_expired_lease_via_outbox(factory: sessionmaker[Session]) -> None:
    """恢复器把租约过期的 RUNNING 任务置 RETRYING 并同事务写重投事件。"""
    job_id = _add_job(factory)
    with session_scope(factory) as session:
        JobService(session).claim_for_run(job_id, lease_ttl_seconds=600)
    _update_job(factory, job_id, lease_expires_at=now_utc() - timedelta(seconds=1))

    with session_scope(factory) as session:
        recovered = recover_expired_leases(session)
        assert recovered == [job_id]

    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.RETRYING
        assert job.last_error is not None
        assert job.last_error["code"] == "LEASE_LOST"
        event = session.scalars(sa.select(OutboxEvent)).first()
        assert event is not None
        assert event.status is OutboxStatus.PENDING
        assert event.payload["job_id"] == str(job_id)
        assert event.payload["task"] == "processing.ingest_raster"
        assert event.next_attempt_at is not None


def test_recovery_ignores_jobs_with_valid_lease(factory: sessionmaker[Session]) -> None:
    job_id = _add_job(factory)
    with session_scope(factory) as session:
        JobService(session).claim_for_run(job_id, lease_ttl_seconds=600)
    with session_scope(factory) as session:
        assert recover_expired_leases(session) == []
    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.RUNNING
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 0


def test_recovery_fails_job_when_attempts_exhausted(
    factory: sessionmaker[Session], tmp_path: Any
) -> None:
    """租约过期且重试次数耗尽：Job 终态 FAILED，关联版本同步终止伪运行状态。"""
    from app.assets.enums import AssetVersionStatus
    from app.assets.models import AssetVersion

    version_id = uuid4()
    job_id = uuid4()
    with session_scope(factory) as session:
        session.add(
            AssetVersion(
                id=version_id,
                asset_id=uuid4(),
                version_number=1,
                status=AssetVersionStatus.PROCESSING,
                original_file_name="fixture.tif",
                size_bytes=8,
                properties={},
            )
        )
        session.add(
            Job(
                id=job_id,
                job_type=JobType.RASTER_INGESTION,
                status=JobStatus.RUNNING,
                payload={"asset_version_id": str(version_id)},
                asset_version_id=version_id,
                attempt=4,
                max_attempts=4,
                lease_token=uuid4(),
                lease_expires_at=now_utc() - timedelta(seconds=1),
            )
        )

    with session_scope(factory) as session:
        assert recover_expired_leases(session) == [job_id]

    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED
        version = session.get(AssetVersion, version_id)
        assert version is not None
        assert version.status is AssetVersionStatus.FAILED
        assert version.diagnostics is not None
        assert version.diagnostics["reason"] == "LEASE_LOST_EXHAUSTED"


def test_schedule_retry_writes_outbox_event_with_backoff(
    factory: sessionmaker[Session],
) -> None:
    """瞬时重试：RETRYING 与重投事件同事务落库，退避写在 next_attempt_at。"""
    job_id = _add_job(factory)
    with session_scope(factory) as session:
        JobService(session).claim_for_run(job_id)

    before = now_utc()
    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        event = JobService(session).schedule_retry(
            job, detail={"code": "TRANSIENT", "detail": "minio 不可用", "transient": True}
        )
        assert event is not None
        assert event.id is not None

    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.RETRYING
        assert job.last_error is not None
        assert job.last_error["code"] == "TRANSIENT"
        event = session.scalars(sa.select(OutboxEvent)).first()
        assert event is not None
        assert event.status is OutboxStatus.PENDING
        assert event.payload["task"] == "processing.ingest_raster"
        # 退避约 5s（attempt=1）；对 next_attempt_at 与取值时刻的差值断言
        assert event.next_attempt_at is not None
        seconds = (_aware(event.next_attempt_at) - before).total_seconds()
        assert 4.0 <= seconds <= 6.0


def test_schedule_retry_marks_failed_when_attempts_exhausted(
    factory: sessionmaker[Session],
) -> None:
    job_id = _add_job(factory, max_attempts=1)
    with session_scope(factory) as session:
        JobService(session).claim_for_run(job_id)  # attempt → 1
    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        event = JobService(session).schedule_retry(
            job, detail={"code": "TRANSIENT", "detail": "再次失败", "transient": True}
        )
        assert event is None
        assert job.status is JobStatus.FAILED
        assert job.last_error is not None
        assert job.last_error["code"] == "TRANSIENT_EXHAUSTED"
    with session_scope(factory) as session:
        # 耗尽路径不产生重投事件
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 0


def test_heartbeat_renews_lease(factory: sessionmaker[Session]) -> None:
    job_id = _add_job(factory)
    with session_scope(factory) as session:
        claim = JobService(session).claim_for_run(job_id, lease_ttl_seconds=1)
        assert claim.lease_token is not None
        original_expiry = claim.job.lease_expires_at
        token = claim.lease_token

    heartbeat = LeaseHeartbeat(
        factory=factory,
        job_id=job_id,
        lease_token=token,  # type: ignore[arg-type]
        interval_seconds=0.05,
        ttl_seconds=1,
    )
    heartbeat.start()
    time.sleep(0.4)
    heartbeat.stop()

    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.lease_expires_at is not None
        assert original_expiry is not None
        assert _aware(job.lease_expires_at) > _aware(original_expiry) + timedelta(seconds=0.2)
        assert job.heartbeat_at is not None


def test_heartbeat_stops_renewing_on_token_mismatch(factory: sessionmaker[Session]) -> None:
    """token 不匹配（已被回收重新认领）时不得覆盖新持有者的租约。"""
    job_id = _add_job(factory)
    with session_scope(factory) as session:
        JobService(session).claim_for_run(job_id, lease_ttl_seconds=600)
    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        expiry_before = job.lease_expires_at

    heartbeat = LeaseHeartbeat(
        factory=factory,
        job_id=job_id,
        lease_token=uuid4(),  # 伪造的旧 token
        interval_seconds=0.05,
        ttl_seconds=600,
    )
    heartbeat.start()
    time.sleep(0.3)
    heartbeat.stop()

    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.lease_expires_at == expiry_before
