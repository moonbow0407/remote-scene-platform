"""监测计划的 PostGIS 空间选择、occurrence 并发幂等、调度锁与 Job+Outbox 同事务。

需要 `APP_INTEGRATION_DATABASE_URL`（已 `alembic upgrade head`）；未提供时跳过。
"""

from __future__ import annotations

import os
import random
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from geoalchemy2 import WKTElement
from sqlalchemy.orm import Session, sessionmaker

from app.assets.enums import AssetStatus, AssetType
from app.assets.models import DataAsset
from app.assets.service import AssetService
from app.db import make_session_factory, session_scope
from app.jobs.enums import JobStatus, JobType, OutboxStatus
from app.jobs.models import Job, OutboxEvent
from app.monitoring.enums import OccurrenceStatus, PlanStatus, RunStatus, ScheduleType
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
        self.calls: list[tuple[int, list[int]]] = []

    def dispatch(
        self, session: Session, run: MonitoringRun, input_version_ids: list[int]
    ) -> int | None:
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
    offset = random.uniform(-170.0, -150.0)

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


def _seed_raster_asset(
    session: Session, *, name: str, footprint_wkt: str | None
) -> DataAsset:
    assets = AssetService(session)
    asset = assets.create_asset(
        name=name,
        asset_type=AssetType.RASTER,
        original_file_name=f"{name}.tif",
        size_bytes=8,
    )
    asset.status = AssetStatus.READY
    if footprint_wkt is not None:
        asset.footprint = WKTElement(footprint_wkt, srid=4326)
    session.flush()
    return asset


def _input_asset_ids(session: Session, run_id: int) -> set[int]:
    return set(
        session.scalars(
            sa.select(MonitoringRunInput.asset_id).where(MonitoringRunInput.run_id == run_id)
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
        row = session.execute(
            sa.select(
                sa.func.ST_SRID(MonitoringPlan.boundary),
                sa.func.ST_IsValid(MonitoringPlan.boundary),
                sa.func.GeometryType(MonitoringPlan.boundary),
            ).where(MonitoringPlan.id == plan.id)
        ).one()
        assert tuple(row) == (4326, True, "MULTIPOLYGON")

        inside = _seed_raster_asset(session, name="inside", footprint_wkt=inside_wkt)
        _seed_raster_asset(session, name="outside", footprint_wkt=outside_wkt)
        run = service.trigger_plan(plan.id)
        assert run.status is RunStatus.PENDING
        assert _input_asset_ids(session, run.id) == {inside.id}
        assert dispatcher.calls[0][1] == [inside.id]


def test_concurrent_ticks_create_exactly_one_occurrence(
    factory: sessionmaker[Session],
) -> None:
    dispatcher = RecordingDispatcher()
    with session_scope(factory) as session:
        service = MonitoringService(session, dispatcher)
        plan = _make_plan(session, service, name="并发幂等集成")
        plan_id = plan.id

    def tick() -> None:
        with session_scope(factory) as session:
            plan = session.get(MonitoringPlan, plan_id)
            assert plan is not None
            plan.next_run_at = T0
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


def test_paused_plan_not_dispatched_on_real_db(factory: sessionmaker[Session]) -> None:
    dispatcher = RecordingDispatcher()
    with session_scope(factory) as session:
        service = MonitoringService(session, dispatcher)
        plan = _make_plan(session, service, name="暂停不派发集成")
        plan.next_run_at = T0
        service.pause_plan(plan.id)
        assert plan.status is PlanStatus.PAUSED
        service.process_due_plans(now=T0 + timedelta(hours=5))
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
    with session_scope(factory) as session:
        service = MonitoringService(session)
        boundary, inside, _outside = _spatial_region()
        plan = _make_plan(session, service, name="真实派发集成", boundary=boundary)
        asset = _seed_raster_asset(session, name="真实派发输入", footprint_wkt=inside)
        run = service.trigger_plan(plan.id)

        assert run.job_id is not None
        job = session.get(Job, run.job_id)
        assert job is not None
        assert job.job_type is JobType.MONITORING_RUN
        assert job.status is JobStatus.PENDING
        assert job.asset_id is None
        assert job.payload["run_id"] == str(run.id)
        event = session.scalar(sa.select(OutboxEvent).where(OutboxEvent.aggregate_id == job.id))
        assert event is not None
        assert event.status is OutboxStatus.PENDING
        assert event.payload["task"] == "monitoring.execute_run"
        assert event.payload["args"] == [str(job.id)]
        assert _input_asset_ids(session, run.id) == {asset.id}
