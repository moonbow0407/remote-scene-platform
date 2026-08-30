"""Stage 5 PostgreSQL/PostGIS 集成测试：高风险基础设施边界。

覆盖（按《阶段迁移实施方案》集成测试接缝）：
- 计划边界的 PostGIS 存储（SRID/合法性/类型归一）与空间相交选择（ST_Intersects）；
- occurrence 唯一约束在真实并发下的幂等（两个 Scheduler 会话竞争同一周期，
  只产生一次执行与一次派发请求）；
- 手动触发路径在真实数据库上的 occurrence/run 落库；
- 真实派发器：Run 与 MONITORING_RUN Job + Outbox 事件同事务落库；
- Scheduler 进程层 pg advisory lock 互斥；
- PAUSED 计划在真实扫描查询下不参与调度。

需要显式提供 `APP_INTEGRATION_DATABASE_URL`（已完成 `alembic upgrade head`
的 PostgreSQL/PostGIS 库）；未提供时整体跳过，禁止用 Mock 替代真实基础设施。
"""

from __future__ import annotations

import os
import random
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
import sqlalchemy as sa
from geoalchemy2 import WKTElement
from sqlalchemy.orm import Session, sessionmaker

from app.assets.enums import AssetSource, AssetType, AssetVersionStatus
from app.assets.models import AssetVersion, RasterAssetVersion
from app.assets.service import AssetService
from app.db import make_session_factory, session_scope
from app.jobs.enums import JobStatus, JobType, OutboxStatus
from app.jobs.models import Job, OutboxEvent
from app.monitoring.enums import (
    OccurrenceStatus,
    OccurrenceTrigger,
    PlanStatus,
    RunStatus,
    ScheduleType,
)
from app.monitoring.models import (
    MonitoringOccurrence,
    MonitoringPlan,
    MonitoringRun,
    MonitoringRunInput,
)
from app.monitoring.schemas import PlanCreate
from app.monitoring.service import MonitoringService

DATABASE_URL = os.getenv("APP_INTEGRATION_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DATABASE_URL is None, reason="未提供 APP_INTEGRATION_DATABASE_URL"),
]

T0 = datetime(2026, 8, 30, tzinfo=UTC)

# 默认计划边界：114°E–115°E, 30°N–31°N（并发/暂停/手动触发用例只验证执行，
# 不依赖空间唯一性；空间选择用例使用 _spatial_region 生成的独立区域）
_BOUNDARY = {
    "type": "Polygon",
    "coordinates": [[[114.0, 30.0], [115.0, 30.0], [115.0, 31.0], [114.0, 31.0], [114.0, 30.0]]],
}


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    assert DATABASE_URL is not None
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True)
    yield make_session_factory(engine)
    engine.dispose()


class RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[UUID, list[UUID]]] = []

    def dispatch(
        self, session: Session, run: MonitoringRun, input_version_ids: list[UUID]
    ) -> UUID | None:
        self.calls.append((run.id, list(input_version_ids)))
        return None


def _make_plan(
    session: Session,
    service: MonitoringService,
    *,
    name: str,
    boundary: dict[str, object] | None = None,
) -> MonitoringPlan:
    return service.create_plan(
        PlanCreate(
            name=name,
            boundary=boundary if boundary is not None else _BOUNDARY,
            schedule_type=ScheduleType.INTERVAL,
            schedule_expression="P1D",
            timezone="UTC",
        )
    )


def _spatial_region() -> tuple[dict[str, object], str, str]:
    """生成一次测试专用的空间区域（随机经度偏移）。

    集成库跨测试/跨重放共享：随机偏移保证本测试的边界与既有数据空间不相交，
    断言可确定地重放。
    """
    offset = random.uniform(-170.0, -150.0)  # 西经随机带：与共享库既有数据（东经 0-121）空间不相交

    def _polygon(x0: float, y0: float) -> str:
        return (
            f"POLYGON(({x0} {y0},{x0 + 0.1} {y0},{x0 + 0.1} {y0 + 0.1},{x0} {y0 + 0.1},{x0} {y0}))"
        )

    boundary = {
        "type": "Polygon",
        "coordinates": [
            [
                [offset, 30.0],
                [offset + 1.0, 30.0],
                [offset + 1.0, 31.0],
                [offset, 31.0],
                [offset, 30.0],
            ]
        ],
    }
    inside = _polygon(offset + 0.4, 30.4)
    outside = _polygon(offset + 3.0, 30.0)
    return boundary, inside, outside


def _seed_raster_version(
    session: Session, *, name: str, footprint_wkt: str | None
) -> AssetVersion:
    assets = AssetService(session)
    asset = assets.create_asset(
        name=name, asset_type=AssetType.RASTER, source=AssetSource.SATELLITE
    )
    version = assets.create_version(
        asset_id=asset.id,
        original_file_name=f"{name}.tif",
        size_bytes=8,
        status=AssetVersionStatus.READY,
    )
    if footprint_wkt is not None:
        session.add(
            RasterAssetVersion(
                asset_version_id=version.id, footprint=WKTElement(footprint_wkt, srid=4326)
            )
        )
    session.flush()
    return version


def _input_version_ids(session: Session, run_id: UUID) -> set[UUID]:
    return set(
        session.scalars(
            sa.select(MonitoringRunInput.asset_version_id).where(
                MonitoringRunInput.run_id == run_id
            )
        )
    )


def test_plan_boundary_stored_in_postgis_and_spatial_selection(
    factory: sessionmaker[Session],
) -> None:
    dispatcher = RecordingDispatcher()
    boundary, inside_wkt, outside_wkt = _spatial_region()
    with session_scope(factory) as session:
        service = MonitoringService(session, dispatcher)
        plan = _make_plan(session, service, name="空间选择集成", boundary=boundary)
        # PostGIS 几何列：SRID 4326、几何合法、Polygon 归一化为 MULTIPOLYGON
        row = session.execute(
            sa.select(
                sa.func.ST_SRID(MonitoringPlan.boundary),
                sa.func.ST_IsValid(MonitoringPlan.boundary),
                sa.func.GeometryType(MonitoringPlan.boundary),
            ).where(MonitoringPlan.id == plan.id)
        ).one()
        assert tuple(row) == (4326, True, "MULTIPOLYGON")

        inside = _seed_raster_version(session, name="inside", footprint_wkt=inside_wkt)
        _seed_raster_version(session, name="outside", footprint_wkt=outside_wkt)
        run = service.trigger_plan(plan.id)
        assert run.status is RunStatus.PENDING
        assert _input_version_ids(session, run.id) == {inside.id}
        assert dispatcher.calls[0][1] == [inside.id]


def test_concurrent_ticks_create_exactly_one_occurrence(
    factory: sessionmaker[Session],
) -> None:
    """两个并发 Scheduler 会话竞争同一计划周期：唯一约束兜底，只产生一次执行。"""
    dispatcher = RecordingDispatcher()
    with session_scope(factory) as session:
        service = MonitoringService(session, dispatcher)
        plan = _make_plan(session, service, name="并发幂等集成")
        plan_id = plan.id

    def tick() -> None:
        with session_scope(factory) as session:
            plan = session.get(MonitoringPlan, plan_id)
            assert plan is not None
            plan.next_run_at = T0  # 两个会话扫描同一个到期周期
            MonitoringService(session, dispatcher).process_due_plans(
                now=T0 + timedelta(minutes=1)
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: tick(), range(2)))

    with session_scope(factory) as session:
        dispatched = list(
            session.scalars(
                sa.select(MonitoringOccurrence).where(
                    MonitoringOccurrence.plan_id == plan_id,
                    MonitoringOccurrence.status == OccurrenceStatus.DISPATCHED,
                )
            )
        )
        assert len(dispatched) == 1
        assert dispatched[0].scheduled_for.replace(tzinfo=UTC) == T0
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(MonitoringRun)
                .where(MonitoringRun.plan_id == plan_id)
            )
            == 1
        )
        assert len(dispatcher.calls) == 1


def test_manual_trigger_creates_occurrence_and_run(
    factory: sessionmaker[Session],
) -> None:
    dispatcher = RecordingDispatcher()
    with session_scope(factory) as session:
        service = MonitoringService(session, dispatcher)
        plan = _make_plan(session, service, name="手动触发集成")
        run = service.trigger_plan(plan.id)
        occurrence = session.get(MonitoringOccurrence, run.occurrence_id)
        assert occurrence is not None
        assert occurrence.trigger is OccurrenceTrigger.MANUAL
        assert occurrence.status is OccurrenceStatus.DISPATCHED
        assert run.status is RunStatus.PENDING
        # 输入快照行与 run 同事务落库；空快照场景由单元测试覆盖
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(MonitoringRun)
                .where(MonitoringRun.id == run.id)
            )
            == 1
        )


def test_paused_plan_not_dispatched_on_real_db(factory: sessionmaker[Session]) -> None:
    dispatcher = RecordingDispatcher()
    with session_scope(factory) as session:
        service = MonitoringService(session, dispatcher)
        plan = _make_plan(session, service, name="暂停不派发集成")
        plan.next_run_at = T0
        service.pause_plan(plan.id)
        assert plan.status is PlanStatus.PAUSED
        service.process_due_plans(now=T0 + timedelta(hours=5))
        # 共享库中可能存在其他测试创建的到期计划，这里只断言本计划未被调度
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(MonitoringOccurrence)
                .where(MonitoringOccurrence.plan_id == plan.id)
            )
            == 0
        )
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(MonitoringRun)
                .where(MonitoringRun.plan_id == plan.id)
            )
            == 0
        )


def test_advisory_lock_is_mutually_exclusive() -> None:
    """Scheduler 进程层互斥：同一时刻只有一个实例能取得锁。"""
    from app.scheduler.main import _release_advisory_lock, _try_advisory_lock

    assert DATABASE_URL is not None
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as first, engine.connect() as second:
            assert _try_advisory_lock(first) is True
            assert _try_advisory_lock(second) is False
            _release_advisory_lock(first)
            assert _try_advisory_lock(second) is True
            _release_advisory_lock(second)
    finally:
        engine.dispose()


def test_trigger_dispatches_real_job_and_outbox(factory: sessionmaker[Session]) -> None:
    """真实派发器：Run 与 MONITORING_RUN Job + Outbox 事件同事务落库。"""
    with session_scope(factory) as session:
        service = MonitoringService(session)  # 默认派发器 JobRunDispatcher
        boundary, inside, _outside = _spatial_region()
        plan = _make_plan(session, service, name="真实派发集成", boundary=boundary)
        version = _seed_raster_version(session, name="真实派发输入", footprint_wkt=inside)
        run = service.trigger_plan(plan.id)

        assert run.job_id is not None
        job = session.get(Job, run.job_id)
        assert job is not None
        assert job.job_type is JobType.MONITORING_RUN
        assert job.status is JobStatus.PENDING
        # 监测执行无单版本引用；权威输入关联在 monitoring_run_input
        assert job.asset_version_id is None
        assert job.payload["run_id"] == str(run.id)
        event = session.scalar(sa.select(OutboxEvent).where(OutboxEvent.aggregate_id == job.id))
        assert event is not None
        assert event.status is OutboxStatus.PENDING
        assert event.payload["task"] == "monitoring.execute_run"
        assert event.payload["args"] == [str(job.id)]
        assert _input_version_ids(session, run.id) == {version.id}
