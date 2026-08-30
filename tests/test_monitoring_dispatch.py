"""Stage 5 派发接线与监测执行核心测试（内存 SQLite，不依赖外部基础设施）。

覆盖：JobRunDispatcher 同事务创建 MONITORING_RUN Job + Outbox、调用方事务
回滚原子性、执行核心的成功/空快照/快照损坏/瞬时重试/重试耗尽/重复投递幂等
/租约回收恢复/payload 损坏终态。

方言处理与 tests/test_monitoring_service.py 相同（JSONB 编译钩子、SpatiaLite
桩、显式 BEGIN）；执行核心不触发空间选择（计划经真实派发器创建，选择查询
由 autouse 夹具置空空间维度）。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection as SqliteConnection
from uuid import UUID

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import Table

from app.assets.enums import AssetSource, AssetType, AssetVersionStatus
from app.assets.models import AssetVersion, DataAsset, ObjectBlob, PropertySchema
from app.assets.service import AssetService
from app.catalogs.models import ResourceCatalog, Satellite, Sensor
from app.db import Base, session_scope
from app.ecology.models import EcologicalParameter, EcologicalParameterResourceMapping
from app.jobs.enums import JobStatus, JobType, OutboxStatus
from app.jobs.models import Job, JobEvent, OutboxEvent
from app.jobs.service import JobService
from app.monitoring.enums import RunStatus
from app.monitoring.execution import execute_monitoring_run
from app.monitoring.models import (
    MonitoringOccurrence,
    MonitoringPlan,
    MonitoringRun,
    MonitoringRunInput,
)
from app.monitoring.schemas import PlanCreate
from app.monitoring.selection import SelectionCriteria
from app.monitoring.service import MonitoringService

T0 = datetime(2026, 8, 30, tzinfo=UTC)

_VALID_BOUNDARY = {
    "type": "Polygon",
    "coordinates": [[[114.0, 30.0], [115.0, 30.0], [115.0, 31.0], [114.0, 31.0], [114.0, 30.0]]],
}


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(_type: JSONB, compiler: object, **_kw: object) -> str:
    return "JSON"


def _make_engine() -> sa.Engine:
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _sqlite_dialect_stubs(dbapi_conn: SqliteConnection, _record: object) -> None:
        for name in (
            "RecoverGeometryColumn",
            "AddGeometryColumn",
            "DisableSpatialIndex",
            "CreateSpatialIndex",
        ):
            dbapi_conn.create_function(name, 5, lambda *args: None)
        dbapi_conn.create_function(
            "GeomFromEWKT", 1, lambda value: value.split(";", 1)[-1] if value else value
        )
        dbapi_conn.create_function("AsEWKB", 1, lambda _value: "0106000020a086010000000000")
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        # 与 test_monitoring_service.py 相同：显式 BEGIN 保住 SAVEPOINT 幂等语义
        dbapi_conn.isolation_level = None

    @event.listens_for(engine, "begin")
    def _sqlite_begin(conn: sa.Connection) -> None:
        conn.exec_driver_sql("BEGIN")

    tables: list[Table] = [
        Base.metadata.tables[name]
        for name in (
            ResourceCatalog.__tablename__,
            Satellite.__tablename__,
            Sensor.__tablename__,
            EcologicalParameter.__tablename__,
            EcologicalParameterResourceMapping.__tablename__,
            ObjectBlob.__tablename__,
            AssetVersion.__tablename__,
            DataAsset.__tablename__,
            PropertySchema.__tablename__,
            Job.__tablename__,
            JobEvent.__tablename__,
            OutboxEvent.__tablename__,
            MonitoringPlan.__tablename__,
            "monitoring_plan_parameter",
            MonitoringOccurrence.__tablename__,
            MonitoringRun.__tablename__,
            MonitoringRunInput.__tablename__,
        )
    ]
    Base.metadata.create_all(engine, tables=tables)
    return engine


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    engine = _make_engine()
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture(autouse=True)
def no_spatial_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite 无 PostGIS：选择查询的空间维度置空，其余过滤逻辑真实执行。"""
    import app.monitoring.service as service_module
    from app.monitoring.selection import select_ready_versions as real_select

    def _without_boundary(session: Session, criteria: SelectionCriteria) -> list[AssetVersion]:
        return real_select(session, replace(criteria, boundary_wkt=None))

    monkeypatch.setattr(service_module, "select_ready_versions", _without_boundary)


def _make_plan(session: Session, service: MonitoringService, *, name: str) -> MonitoringPlan:
    return service.create_plan(
        PlanCreate(
            name=name,
            boundary=_VALID_BOUNDARY,
            schedule_type="INTERVAL",
            schedule_expression="P1D",
            timezone="UTC",
        )
    )


def _seed_ready_version(session: Session, *, name: str) -> AssetVersion:
    assets = AssetService(session)
    asset = assets.create_asset(
        name=name, asset_type=AssetType.RASTER, source=AssetSource.SATELLITE
    )
    return assets.create_version(
        asset_id=asset.id,
        original_file_name=f"{name}.tif",
        size_bytes=8,
        status=AssetVersionStatus.READY,
    )


def _trigger(session: Session, *, name: str) -> tuple[MonitoringService, MonitoringRun]:
    """创建计划并手动触发一次执行（真实派发器，Job+Outbox 同事务落库）。"""
    service = MonitoringService(session)
    plan = _make_plan(session, service, name=name)
    _seed_ready_version(session, name=f"{name}-input")
    run = service.trigger_plan(plan.id)
    session.flush()
    return service, run


def _outbox_of(session: Session, job_id: UUID) -> OutboxEvent:
    return session.scalar(sa.select(OutboxEvent).where(OutboxEvent.aggregate_id == job_id))


class TestJobRunDispatcher:
    def test_dispatch_creates_job_and_outbox_in_same_transaction(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            _service, run = _trigger(session, name="派发原子性")
            session.flush()

            assert run.job_id is not None
            job = session.get(Job, run.job_id)
            assert job is not None
            assert job.job_type is JobType.MONITORING_RUN
            assert job.status is JobStatus.PENDING
            # 监测执行无单版本引用；权威输入关联在 monitoring_run_input
            assert job.asset_version_id is None
            assert job.payload["run_id"] == str(run.id)

            event = _outbox_of(session, job.id)
            assert event is not None
            assert event.event_type == "job.dispatch"
            assert event.status is OutboxStatus.PENDING
            assert event.payload["task"] == "monitoring.execute_run"
            assert event.payload["args"] == [str(job.id)]

            created = session.scalar(
                sa.select(JobEvent).where(
                    JobEvent.job_id == job.id, JobEvent.event_type == "JOB_CREATED"
                )
            )
            assert created is not None

            inputs = list(
                session.scalars(
                    sa.select(MonitoringRunInput).where(MonitoringRunInput.run_id == run.id)
                )
            )
            assert len(inputs) == 1

    def test_dispatch_rolls_back_with_caller_transaction(
        self, factory: sessionmaker[Session]
    ) -> None:
        """派发失败/调用方回滚必须连带撤销 occurrence、Run、快照、Job 与 Outbox。"""
        with (
            pytest.raises(RuntimeError, match="主动回滚"),
            session_scope(factory) as session,
        ):
            _trigger(session, name="回滚原子性")
            raise RuntimeError("主动回滚")

        with session_scope(factory) as session:
            assert session.scalar(sa.select(sa.func.count()).select_from(MonitoringPlan)) == 0
            assert session.scalar(sa.select(sa.func.count()).select_from(MonitoringRun)) == 0
            assert session.scalar(sa.select(sa.func.count()).select_from(Job)) == 0
            assert session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 0


class TestExecuteMonitoringRun:
    def test_success_advances_run_and_job_together(
        self, factory: sessionmaker[Session]
    ) -> None:
        with session_scope(factory) as session:
            _service, run = _trigger(session, name="执行成功")
            job_id = run.job_id
            assert job_id is not None

        execute_monitoring_run(str(job_id), factory=factory)

        with session_scope(factory) as session:
            run_after = session.get(MonitoringRun, run.id)
            job = session.get(Job, job_id)
            assert run_after is not None and job is not None
            assert run_after.status is RunStatus.SUCCEEDED
            assert run_after.diagnostics is None
            assert job.status is JobStatus.SUCCEEDED
            plan = session.get(MonitoringPlan, run_after.plan_id)
            assert plan is not None
            occurrence = session.get(MonitoringOccurrence, run_after.occurrence_id)
            assert occurrence is not None
            # 成功推进计划最近成功时刻（供展示）；增量锚点以 window_anchor 为准
            assert plan.last_successful_run_at == occurrence.scheduled_for

    def test_empty_snapshot_succeeds(self, factory: sessionmaker[Session]) -> None:
        """窗口内无合格版本时快照为空，执行仍成功（"执行了，无新增数据"）。"""
        with session_scope(factory) as session:
            service = MonitoringService(session)
            plan = _make_plan(session, service, name="空快照")
            run = service.trigger_plan(plan.id)
            session.flush()
            assert run.job_id is not None
            inputs = list(
                session.scalars(
                    sa.select(MonitoringRunInput).where(MonitoringRunInput.run_id == run.id)
                )
            )
            assert inputs == []
            job_id = run.job_id

        execute_monitoring_run(str(job_id), factory=factory)

        with session_scope(factory) as session:
            run_after = session.get(MonitoringRun, run.id)
            assert run_after is not None
            assert run_after.status is RunStatus.SUCCEEDED

    def test_broken_snapshot_fails_deterministically(
        self, factory: sessionmaker[Session]
    ) -> None:
        """快照引用的版本在执行前被置为非 READY：Run 与 Job 一同 FAILED，不重试。"""
        with session_scope(factory) as session:
            _service, run = _trigger(session, name="快照损坏")
            job_id = run.job_id
            assert job_id is not None
            version_id = session.scalar(
                sa.select(MonitoringRunInput.asset_version_id).where(
                    MonitoringRunInput.run_id == run.id
                )
            )
            assert version_id is not None
            version = session.get(AssetVersion, version_id)
            assert version is not None
            version.status = AssetVersionStatus.FAILED

        execute_monitoring_run(str(job_id), factory=factory)

        with session_scope(factory) as session:
            run_after = session.get(MonitoringRun, run.id)
            job = session.get(Job, job_id)
            assert run_after is not None and job is not None
            assert run_after.status is RunStatus.FAILED
            assert run_after.diagnostics is not None
            assert run_after.diagnostics["code"] == "SNAPSHOT_BROKEN"
            assert job.status is JobStatus.FAILED
            assert job.last_error is not None
            assert job.last_error["code"] == "SNAPSHOT_BROKEN"
            assert job.last_error["transient"] is False

    def test_transient_error_schedules_retry_then_recovers(
        self, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """审计期数据库瞬时错误：Job RETRYING + Outbox 退避重投，重投后恢复成功。"""
        import app.monitoring.execution as execution_module

        def _boom(session: Session, run: MonitoringRun) -> list[str]:
            raise sa.exc.OperationalError("stmt", {}, Exception("数据库暂不可达"))

        original_verify = execution_module._verify_snapshot
        monkeypatch.setattr(execution_module, "_verify_snapshot", _boom)

        with session_scope(factory) as session:
            _service, run = _trigger(session, name="瞬时重试")
            job_id = run.job_id
            assert job_id is not None

        execute_monitoring_run(str(job_id), factory=factory)

        with session_scope(factory) as session:
            job = session.get(Job, job_id)
            assert job is not None
            assert job.status is JobStatus.RETRYING
            events = list(
                session.scalars(
                    sa.select(OutboxEvent)
                    .where(OutboxEvent.aggregate_id == job_id)
                    .order_by(OutboxEvent.created_at)
                )
            )
            # 首次派发事件 + 瞬时重投事件
            assert len(events) == 2
            assert events[-1].status is OutboxStatus.PENDING
            assert events[-1].next_attempt_at is not None  # 指数退避由 Outbox 承载

        # 精确恢复被注入的函数（不能用 undo()：会连带撤销 autouse 空间夹具）
        monkeypatch.setattr(execution_module, "_verify_snapshot", original_verify)
        execute_monitoring_run(str(job_id), factory=factory)

        with session_scope(factory) as session:
            run_after = session.get(MonitoringRun, run.id)
            job = session.get(Job, job_id)
            assert run_after is not None and job is not None
            assert run_after.status is RunStatus.SUCCEEDED
            assert job.status is JobStatus.SUCCEEDED

    def test_transient_exhaustion_fails_run_and_job(
        self, factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """瞬时错误重试次数耗尽：schedule_retry 置 Job FAILED，Run 同步 FAILED。"""
        import app.monitoring.execution as execution_module

        def _boom(session: Session, run: MonitoringRun) -> list[str]:
            raise sa.exc.OperationalError("stmt", {}, Exception("数据库暂不可达"))

        monkeypatch.setattr(execution_module, "_verify_snapshot", _boom)

        with session_scope(factory) as session:
            _service, run = _trigger(session, name="重试耗尽")
            job_id = run.job_id
            assert job_id is not None
            job = session.get(Job, job_id)
            assert job is not None
            job.attempt = 1
            job.max_attempts = 1

        execute_monitoring_run(str(job_id), factory=factory)

        with session_scope(factory) as session:
            run_after = session.get(MonitoringRun, run.id)
            job = session.get(Job, job_id)
            assert run_after is not None and job is not None
            assert run_after.status is RunStatus.FAILED
            assert job.status is JobStatus.FAILED
            assert job.last_error is not None
            assert job.last_error["code"] == "TRANSIENT_EXHAUSTED"

    def test_redelivery_after_success_is_noop(self, factory: sessionmaker[Session]) -> None:
        """成功落库后的重复消息：不重复审计、不改变任何状态。"""
        with session_scope(factory) as session:
            _service, run = _trigger(session, name="重复投递")
            job_id = run.job_id
            assert job_id is not None

        execute_monitoring_run(str(job_id), factory=factory)
        execute_monitoring_run(str(job_id), factory=factory)

        with session_scope(factory) as session:
            run_after = session.get(MonitoringRun, run.id)
            job = session.get(Job, job_id)
            assert run_after is not None and job is not None
            assert run_after.status is RunStatus.SUCCEEDED
            assert job.status is JobStatus.SUCCEEDED
            events = list(
                session.scalars(
                    sa.select(JobEvent.event_type)
                    .where(JobEvent.job_id == job_id)
                    .order_by(JobEvent.created_at)
                )
            )
            assert events.count("JOB_SUCCEEDED") == 1

    def test_stale_lease_reclaim_resumes_run(self, factory: sessionmaker[Session]) -> None:
        """执行者崩溃后租约过期：恢复重投后从 RUNNING 继续直至成功。"""
        with session_scope(factory) as session:
            _service, run = _trigger(session, name="租约恢复")
            job_id = run.job_id
            assert job_id is not None
            # 模拟上次尝试：已认领（RUNNING）并标记 Run 开始，随后崩溃且未续约
            claim = JobService(session).claim_for_run(job_id)
            assert claim.acquired
            MonitoringService(session).mark_run_started(run.id)
            job = session.get(Job, job_id)
            assert job is not None
            job.lease_expires_at = T0 - timedelta(seconds=1)
            job.lease_token = UUID(int=0)

        execute_monitoring_run(str(job_id), factory=factory)

        with session_scope(factory) as session:
            run_after = session.get(MonitoringRun, run.id)
            job = session.get(Job, job_id)
            assert run_after is not None and job is not None
            assert run_after.status is RunStatus.SUCCEEDED
            assert job.status is JobStatus.SUCCEEDED

    def test_corrupt_payload_fails_job_deterministically(
        self, factory: sessionmaker[Session]
    ) -> None:
        """payload 缺少 run_id：不进入租约重试循环，Job 直接 FAILED。"""
        job_id: UUID | None = None
        with session_scope(factory) as session:
            job, _event = JobService(session).create_job_with_outbox(
                job_type=JobType.MONITORING_RUN, payload={}
            )
            job_id = job.id

        execute_monitoring_run(str(job_id), factory=factory)

        with session_scope(factory) as session:
            job = session.get(Job, job_id)
            assert job is not None
            assert job.status is JobStatus.FAILED
            assert job.last_error is not None
            assert job.last_error["code"] == "MONITORING_PAYLOAD_CORRUPT"
