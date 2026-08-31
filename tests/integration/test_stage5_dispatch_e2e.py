"""监测全链路：计划 → 派发 → Outbox → RabbitMQ → Geo Worker → 终态。

需要同时提供：
- APP_INTEGRATION_DATABASE_URL：空 PostgreSQL/PostGIS 实例；测试创建一次性库并升级到 head；
- APP_INTEGRATION_RABBITMQ_URL：amqp://用户:口令@主机:端口/。
未提供时整体跳过。Dispatcher 与 Worker 以子进程运行。
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from geoalchemy2 import WKTElement
from sqlalchemy.orm import Session, sessionmaker

from app.assets.enums import AssetStatus, AssetType
from app.assets.service import AssetService
from app.context import now_utc
from app.db import make_session_factory, session_scope
from app.jobs.enums import JobStatus, OutboxStatus
from app.jobs.models import Job, OutboxEvent
from app.monitoring.enums import RunStatus, ScheduleType
from app.monitoring.models import MonitoringRun, MonitoringRunInput
from app.monitoring.schemas import PlanCreate
from app.monitoring.service import MonitoringService

REPO_ROOT = Path(__file__).resolve().parents[2]

DATABASE_URL = os.getenv("APP_INTEGRATION_DATABASE_URL")
RABBITMQ_URL = os.getenv("APP_INTEGRATION_RABBITMQ_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DATABASE_URL is None, reason="未提供 APP_INTEGRATION_DATABASE_URL"),
    pytest.mark.skipif(RABBITMQ_URL is None, reason="未提供 APP_INTEGRATION_RABBITMQ_URL"),
]

_READY_SECONDS = 8.0
_TERMINAL_TIMEOUT_SECONDS = 90.0


@pytest.fixture(scope="module")
def e2e_env(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[sessionmaker[Session], dict[str, Path]]]:
    """一次性数据库 + 真实 Dispatcher/Worker 子进程。"""
    assert DATABASE_URL is not None and RABBITMQ_URL is not None

    admin_engine = sa.create_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    dbname = f"stage5_e2e_{uuid4().hex[:12]}"
    with admin_engine.connect() as conn:
        conn.execute(sa.text(f'CREATE DATABASE "{dbname}"'))
    admin_engine.dispose()
    e2e_url = DATABASE_URL.rsplit("/", 1)[0] + f"/{dbname}"

    os.environ["APP_DATABASE_URL"] = e2e_url
    from app.settings import get_settings

    get_settings.cache_clear()
    from alembic.config import Config

    from alembic import command

    config = Config(str(REPO_ROOT / "alembic.ini"))
    command.upgrade(config, "head")

    engine = sa.create_engine(e2e_url, pool_pre_ping=True)
    factory = make_session_factory(engine)

    env = {
        **os.environ,
        "APP_DATABASE_URL": e2e_url,
        "APP_RABBITMQ_URL": RABBITMQ_URL,
    }
    log_dir = tmp_path_factory.mktemp("stage5-e2e")
    logs = {
        "dispatcher": log_dir / "dispatcher.log",
        "worker": log_dir / "worker.log",
    }
    with open(logs["dispatcher"], "wb") as dispatcher_log, open(logs["worker"], "wb") as worker_log:
        dispatcher = subprocess.Popen(
            [sys.executable, "-m", "app.dispatcher.main"],
            cwd=REPO_ROOT,
            env=env,
            stdout=dispatcher_log,
            stderr=subprocess.STDOUT,
        )
        worker = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "celery",
                "-A",
                "app.worker.celery_app:celery",
                "worker",
                "-P",
                "solo",
                "-Q",
                "geo",
                "--loglevel",
                "WARNING",
                "--without-gossip",
                "--without-mingle",
                "--without-heartbeat",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=worker_log,
            stderr=subprocess.STDOUT,
        )
        time.sleep(_READY_SECONDS)
        yield factory, logs
        for process in (worker, dispatcher):
            process.terminate()
        for process in (worker, dispatcher):
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    engine.dispose()
    cleanup_engine = sa.create_engine(DATABASE_URL, isolation_level="AUTOCOMMIT")
    with cleanup_engine.connect() as conn:
        conn.execute(
            sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :name AND pid <> pg_backend_pid()"
            ),
            {"name": dbname},
        )
        conn.execute(sa.text(f'DROP DATABASE "{dbname}"'))
    cleanup_engine.dispose()


def _make_plan(
    session: Session, service: MonitoringService, *, name: str, boundary: dict[str, object]
) -> int:
    plan = service.create_plan(
        PlanCreate(
            name=name,
            boundary=boundary,
            schedule_type=ScheduleType.INTERVAL,
            schedule_expression="P1D",
            timezone="UTC",
        )
    )
    return plan.id


def _seed_ready_asset(session: Session, *, name: str, inside_wkt: str) -> int:
    assets = AssetService(session)
    asset = assets.create_asset(
        name=name,
        asset_type=AssetType.RASTER,
        original_file_name=f"{name}.tif",
        size_bytes=8,
    )
    asset.status = AssetStatus.READY
    asset.footprint = WKTElement(inside_wkt, srid=4326)
    session.flush()
    return asset.id


def _region() -> tuple[dict[str, object], str, str]:
    offset = random.randint(0, 100)

    def polygon(x0: float, y0: float) -> str:
        return (
            f"POLYGON(({x0} {y0},{x0 + 0.1} {y0},{x0 + 0.1} {y0 + 0.1},"
            f"{x0} {y0 + 0.1},{x0} {y0}))"
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
    return boundary, polygon(offset + 0.2, 30.2), polygon(offset + 0.6, 30.6)


def _wait_for_run_terminal(factory: sessionmaker[Session], run_id: int) -> str:
    deadline = time.monotonic() + _TERMINAL_TIMEOUT_SECONDS
    last: str | None = None
    while time.monotonic() < deadline:
        with session_scope(factory) as session:
            run = session.get(MonitoringRun, run_id)
            if run is not None:
                last = run.status.value
                if run.status in (RunStatus.SUCCEEDED, RunStatus.FAILED):
                    return last
        time.sleep(1.0)
    pytest.fail(f"Run {run_id} 在 {_TERMINAL_TIMEOUT_SECONDS}s 内未到终态（最后状态 {last}）")


def _wait_for_outbox_published(factory: sessionmaker[Session], job_id: int) -> None:
    deadline = time.monotonic() + 30.0
    last: str | None = None
    while time.monotonic() < deadline:
        with session_scope(factory) as session:
            events = list(
                session.scalars(
                    sa.select(OutboxEvent)
                    .where(OutboxEvent.aggregate_id == job_id)
                    .order_by(OutboxEvent.created_at)
                )
            )
            if events:
                last = events[0].status.value
                if events[0].status is OutboxStatus.PUBLISHED:
                    return
        time.sleep(1.0)
    pytest.fail(f"Job {job_id} 的首个派发事件未在期限内发布（最后状态 {last}）")


def test_full_chain_trigger_to_worker_succeeds(
    e2e_env: tuple[sessionmaker[Session], dict[str, Path]],
) -> None:
    factory, _logs = e2e_env
    boundary, inside, _second = _region()
    with session_scope(factory) as session:
        service = MonitoringService(session)
        plan_id = _make_plan(session, service, name="全链路E2E计划", boundary=boundary)
        version_id = _seed_ready_asset(session, name="全链路E2E输入", inside_wkt=inside)
        run = service.trigger_plan(plan_id)
        assert run.job_id is not None
        plan_and_job = (plan_id, run.id, run.job_id, version_id)

    _plan_id, run_id, job_id, version_id = plan_and_job
    assert _wait_for_run_terminal(factory, run_id) == "SUCCEEDED"
    _wait_for_outbox_published(factory, job_id)

    with session_scope(factory) as session:
        run = session.get(MonitoringRun, run_id)
        job = session.get(Job, job_id)
        assert run is not None and job is not None
        assert run.status is RunStatus.SUCCEEDED
        assert run.finished_at is not None
        assert run.diagnostics is None
        assert job.status is JobStatus.SUCCEEDED
        assert job.asset_id is None
        events = list(
            session.scalars(
                sa.select(OutboxEvent)
                .where(OutboxEvent.aggregate_id == job_id)
                .order_by(OutboxEvent.created_at)
            )
        )
        assert events, "未找到派发事件"
        assert events[0].status is OutboxStatus.PUBLISHED
        assert events[0].published_at is not None
        inputs = set(
            session.scalars(
                sa.select(MonitoringRunInput.asset_id).where(
                    MonitoringRunInput.run_id == run_id
                )
            )
        )
        assert inputs == {version_id}


def test_second_run_selects_only_new_versions(
    e2e_env: tuple[sessionmaker[Session], dict[str, Path]],
) -> None:
    factory, _logs = e2e_env
    boundary, first_inside, second_inside = _region()
    with session_scope(factory) as session:
        service = MonitoringService(session)
        plan_id = _make_plan(session, service, name="增量E2E计划", boundary=boundary)
        first_version = _seed_ready_asset(
            session, name="增量E2E第一批", inside_wkt=first_inside
        )
        run1 = service.trigger_plan(plan_id)
        assert run1.job_id is not None
        run1_id = run1.id

    assert _wait_for_run_terminal(factory, run1_id) == "SUCCEEDED"

    with session_scope(factory) as session:
        service = MonitoringService(session)
        second_version = _seed_ready_asset(
            session, name="增量E2E第二批", inside_wkt=second_inside
        )
        plan = service.get_plan_required(plan_id)
        plan.next_run_at = now_utc()
        summary = service.process_due_plans(now=now_utc() + timedelta(seconds=5))
        assert summary.dispatched == 1
        runs = list(
            session.scalars(
                sa.select(MonitoringRun)
                .where(MonitoringRun.plan_id == plan_id)
                .order_by(MonitoringRun.created_at)
            )
        )
        assert len(runs) == 2
        run2_id = runs[-1].id
        run2_inputs = set(
            session.scalars(
                sa.select(MonitoringRunInput.asset_id).where(
                    MonitoringRunInput.run_id == run2_id
                )
            )
        )

    assert _wait_for_run_terminal(factory, run2_id) == "SUCCEEDED"
    assert run2_inputs == {second_version}
    assert first_version != second_version
