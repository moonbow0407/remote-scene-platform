"""缺失 Job/Run 必须 ACK；删除计划/资产必须同时收掉 Job 与 Outbox。

这两个缺陷都会把无法完成的消息留在共享 geo 队列里（acks_late + 重试），
阻塞入库与监测执行。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from geoalchemy2 import Geometry, WKTElement
from geoalchemy2.admin.dialects import sqlite as ga_sqlite
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.monitoring.execution as monitoring_execution
import app.processing.tasks as processing_tasks
from app.assets.enums import AssetStatus, AssetType
from app.assets.lifecycle import AssetLifecycleService
from app.assets.models import DataAsset
from app.db import Base, session_scope
from app.jobs.enums import JobStatus, JobType
from app.jobs.models import Job, JobEvent, OutboxEvent
from app.jobs.service import JobService, OutboxRepository
from app.monitoring.enums import (
    OccurrenceStatus,
    OccurrenceTrigger,
    PlanStatus,
    RunStatus,
    ScheduleType,
)
from app.monitoring.execution import execute_monitoring_run
from app.monitoring.models import MonitoringOccurrence, MonitoringPlan, MonitoringRun
from app.monitoring.service import MonitoringService


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(_type: JSONB, compiler: object, **_kw: object) -> str:
    return "JSON"


@compiles(Geometry, "sqlite")
def _geometry_sqlite(_type: Geometry, compiler: object, **_kw: object) -> str:
    return "TEXT"


_JOB_TABLES = ("job", "job_event", "outbox_event")
_ASSET_TABLES = ("data_asset", "object_cleanup_task", *_JOB_TABLES)
_PLAN_TABLES = (
    "data_asset",
    *_JOB_TABLES,
    "monitoring_plan",
    "monitoring_plan_parameter",
    "monitoring_occurrence",
    "monitoring_run",
    "monitoring_run_input",
)

_WORKER_SETTINGS = SimpleNamespace(
    job_lease_ttl_seconds=600,
    job_heartbeat_interval_seconds=60,
    worker_tmp_dir="/tmp",
    worker_task_soft_timeout_seconds=21600,
)


@pytest.fixture(autouse=True)
def _disable_spatialite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ga_sqlite, "after_create", lambda *_a, **_k: None)
    monkeypatch.setattr(ga_sqlite, "before_drop", lambda *_a, **_k: None)


def _engine_for(tables: tuple[str, ...]) -> sa.Engine:
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _stub_geometry_binds(dbapi_conn: Any, _record: object) -> None:
        dbapi_conn.create_function(
            "GeomFromEWKT", 1, lambda value: value.split(";", 1)[-1] if value else value
        )
        dbapi_conn.create_function("AsEWKB", 1, lambda _value: "0106000020a086010000000000")

    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in tables])
    return engine


@pytest.fixture()
def job_factory() -> Iterator[sessionmaker[Session]]:
    engine = _engine_for(_JOB_TABLES)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def asset_factory() -> Iterator[sessionmaker[Session]]:
    engine = _engine_for(_ASSET_TABLES)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture()
def plan_factory() -> Iterator[sessionmaker[Session]]:
    engine = _engine_for(_PLAN_TABLES)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def _add_job(
    session: Session,
    *,
    job_type: JobType = JobType.RASTER_INGESTION,
    asset_id: int | None = 1,
    payload: dict[str, Any] | None = None,
) -> Job:
    job, _event = JobService(session).create_job_with_outbox(
        job_type=job_type,
        payload=payload or {"asset_id": "1", "source_object_key": "k", "source_size_bytes": 8},
        asset_id=asset_id,
    )
    return job


class _MustNotRun:
    def __init__(self, **_kwargs: object) -> None:
        raise AssertionError("Job 已删除时不得启动入库")

    def run(self, _ctx: object) -> None:
        raise AssertionError("Job 已删除时不得执行入库")


def test_claim_for_run_returns_none_when_job_missing(job_factory: sessionmaker[Session]) -> None:
    with session_scope(job_factory) as session:
        assert JobService(session).claim_for_run(999) is None


def test_delete_jobs_and_outbox_removes_both(job_factory: sessionmaker[Session]) -> None:
    with session_scope(job_factory) as session:
        keep = _add_job(
            session,
            payload={"asset_id": "1", "source_object_key": "a", "source_size_bytes": 1},
        )
        drop = _add_job(
            session,
            asset_id=2,
            payload={"asset_id": "2", "source_object_key": "b", "source_size_bytes": 1},
        )
        keep_id, drop_id = keep.id, drop.id
        JobService(session).delete_jobs_and_outbox([drop_id])

    with session_scope(job_factory) as session:
        assert session.get(Job, drop_id) is None
        assert session.get(Job, keep_id) is not None
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == drop_id)
            )
            == 0
        )
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == keep_id)
            )
            == 1
        )
        assert (
            session.scalar(
                sa.select(sa.func.count()).select_from(JobEvent).where(JobEvent.job_id == drop_id)
            )
            == 0
        )


def test_discard_missing_jobs_drops_orphaned_outbox(job_factory: sessionmaker[Session]) -> None:
    with session_scope(job_factory) as session:
        job = _add_job(session)
        event = session.scalar(sa.select(OutboxEvent).where(OutboxEvent.aggregate_id == job.id))
        assert event is not None
        session.delete(job)
        session.flush()
        event_id = event.id
        remaining = OutboxRepository(session).discard_missing_jobs([event])
        assert remaining == []
        session.expunge_all()
        assert session.get(OutboxEvent, event_id) is None


def test_ingest_missing_job_returns_without_raising(
    job_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(processing_tasks, "get_settings", lambda: _WORKER_SETTINGS)
    monkeypatch.setattr(processing_tasks, "_get_factory", lambda: job_factory)
    processing_tasks._execute_ingestion(None, "404", _MustNotRun, "栅格")


def test_monitoring_missing_job_returns_without_raising(
    job_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitoring_execution, "get_settings", lambda: _WORKER_SETTINGS)
    execute_monitoring_run("404", factory=job_factory)


def test_monitoring_missing_run_fails_job_and_acks(
    plan_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(monitoring_execution, "get_settings", lambda: _WORKER_SETTINGS)
    with session_scope(plan_factory) as session:
        job = _add_job(
            session,
            job_type=JobType.MONITORING_RUN,
            asset_id=None,
            payload={"run_id": "51", "plan_id": "1", "input_count": 0},
        )
        job_id = job.id

    execute_monitoring_run(str(job_id), factory=plan_factory)

    with session_scope(plan_factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.FAILED
        assert job.last_error is not None
        assert job.last_error["code"] == "MONITORING_RUN_GONE"


def test_purge_asset_removes_job_and_outbox(
    asset_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.monitoring.service.MonitoringService.asset_has_snapshot_references",
        lambda self, asset_id: False,
    )
    now = datetime(2026, 8, 30, tzinfo=UTC)
    with session_scope(asset_factory) as session:
        asset = DataAsset(
            name="待清理",
            asset_type=AssetType.ATTACHMENT,
            status=AssetStatus.READY,
            original_file_name="a.bin",
            size_bytes=4,
            original_object_key="original/a.bin",
        )
        session.add(asset)
        session.flush()
        job = _add_job(
            session,
            asset_id=asset.id,
            payload={
                "asset_id": str(asset.id),
                "source_object_key": "original/a.bin",
                "source_size_bytes": 4,
            },
        )
        asset_id, job_id = asset.id, job.id
        AssetLifecycleService(session).soft_delete(
            asset_id, retention_days=7, now=now - timedelta(days=8)
        )

    with session_scope(asset_factory) as session:
        assert AssetLifecycleService(session).purge_asset(asset_id, now=now)
        assert session.get(DataAsset, asset_id) is None
        assert session.get(Job, job_id) is None
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == job_id)
            )
            == 0
        )


def test_delete_plan_removes_job_and_outbox(plan_factory: sessionmaker[Session]) -> None:
    with session_scope(plan_factory) as session:
        plan = MonitoringPlan(
            name="待删计划",
            status=PlanStatus.ACTIVE,
            boundary=WKTElement("MULTIPOLYGON(((0 0,1 0,1 1,0 1,0 0)))", srid=4326),
            boundary_wkt="MULTIPOLYGON(((0 0,1 0,1 1,0 1,0 0)))",
            schedule_type=ScheduleType.INTERVAL,
            schedule_expression="P1D",
            timezone="UTC",
        )
        session.add(plan)
        session.flush()
        occurrence = MonitoringOccurrence(
            plan_id=plan.id,
            scheduled_for=datetime(2026, 8, 30, tzinfo=UTC),
            trigger=OccurrenceTrigger.MANUAL,
            status=OccurrenceStatus.DISPATCHED,
        )
        session.add(occurrence)
        session.flush()
        job = _add_job(
            session,
            job_type=JobType.MONITORING_RUN,
            asset_id=None,
            payload={"run_id": "0", "plan_id": str(plan.id), "input_count": 0},
        )
        run = MonitoringRun(
            plan_id=plan.id,
            occurrence_id=occurrence.id,
            status=RunStatus.PENDING,
            window_anchor=datetime(2026, 8, 30, tzinfo=UTC),
            job_id=job.id,
        )
        session.add(run)
        session.flush()
        plan_id, job_id = plan.id, job.id

        MonitoringService(session).delete_plan(plan_id)

        assert session.get(MonitoringPlan, plan_id) is None
        assert session.get(Job, job_id) is None
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id == job_id)
            )
            == 0
        )
