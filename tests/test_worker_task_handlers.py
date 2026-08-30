"""Worker 任务处理器回归：真正经过 _execute_ingestion 的状态落库。

核心回归（A2.5 断链）：版本仍处于 VALIDATING 时缺 CRS 抛 NeedsInputError，
Worker 捕获后必须能同时把 Job 与版本落为 NEEDS_INPUT——旧版本状态机没有
VALIDATING → NEEDS_INPUT 边，导致整个落库事务回滚、任务永久卡 RUNNING。

同时覆盖瞬时重试改走 Outbox（不再调用 Celery self.retry）与各终态落库。
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import app.processing.tasks as tasks
from app.assets.enums import AssetSource, AssetType, AssetVersionStatus
from app.assets.models import AssetVersion
from app.assets.service import AssetService
from app.db import Base, session_scope
from app.jobs.enums import JobStatus, JobType, OutboxStatus
from app.jobs.models import Job, JobEvent, OutboxEvent
from app.jobs.service import JobService
from app.processing.errors import DeterministicError, NeedsInputError, TransientError
from app.settings import Settings


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(_type: JSONB, compiler: object, **_kw: object) -> str:
    return "JSON"


_TABLES = (
    "property_schema",
    "data_asset",
    "asset_version",
    "object_blob",
    "asset_artifact",
    "raster_asset_version",
    "job",
    "job_event",
    "outbox_event",
)


def _sqlite_after_create_without_spatialite(table: Any, bind: Any, **_kw: object) -> None:
    """与 test_raster_inspect_geolocation 相同：替换 geoalchemy2 的 SpatiaLite 钩子。"""
    table.columns = table.info.pop("_saved_columns")
    for column in table.columns:
        actual_type = getattr(column, "_actual_type", None)
        if actual_type is not None:
            column.type = actual_type
            del column._actual_type


@pytest.fixture()
def factory(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Iterator[sessionmaker[Session]]:
    from geoalchemy2.admin import dialects as ga_dialects

    monkeypatch.setattr(ga_dialects.sqlite, "after_create", _sqlite_after_create_without_spatialite)
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _stub_spatialite(dbapi_conn: Any, _record: object) -> None:
        for name in ("AsEWKB", "GeomFromEWKB", "GeomFromEWKT"):
            dbapi_conn.create_function(name, 1, lambda value: value)

    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in _TABLES])
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    # Worker 处理器经模块级 get_settings/_get_factory/MinioAdapter 取运行时依赖
    settings = Settings(worker_tmp_dir=str(tmp_path))
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "_get_factory", lambda: session_factory)
    monkeypatch.setattr(tasks, "MinioAdapter", lambda settings: object())
    yield session_factory
    engine.dispose()


class _NeedsInputRunner:
    def __init__(self, *, settings: object, minio: object, engine: object) -> None:
        pass

    def run(self, ctx: object) -> None:
        raise NeedsInputError(reason="MISSING_CRS", detail="源文件缺少 CRS 且未提供补充信息")


class _TransientRunner:
    def __init__(self, *, settings: object, minio: object, engine: object) -> None:
        pass

    def run(self, ctx: object) -> None:
        raise TransientError("MinIO 暂时不可达")


class _DeterministicRunner:
    def __init__(self, *, settings: object, minio: object, engine: object) -> None:
        pass

    def run(self, ctx: object) -> None:
        raise DeterministicError("UNSUPPORTED_FORMAT", "文件魔数不是 GeoTIFF")


class _SuccessRunner:
    """模拟流水线成功：最后两步把版本置 READY、Job 置 SUCCEEDED。"""

    def __init__(self, *, settings: object, minio: object, engine: object) -> None:
        self._engine = engine

    def run(self, ctx: Any) -> None:
        with session_scope(self._engine) as session:  # type: ignore[arg-type]
            assets = AssetService(session)
            version = assets.get_version_by_id(ctx.version_id)
            assert version is not None
            assets.set_version_status(version, AssetVersionStatus.PROCESSING)
            assets.set_version_status(version, AssetVersionStatus.READY)
            job = JobService(session).get(ctx.job_id)
            assert job is not None
            JobService(session).transition(job, JobStatus.SUCCEEDED, event_type="JOB_SUCCEEDED")


_FORBID_RETRY_SELF = SimpleNamespace(
    retry=lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("不得使用 Celery self.retry 负责重投")
    )
)


def _prepare(factory: sessionmaker[Session], *, max_attempts: int = 4) -> tuple[UUID, UUID]:
    """创建资产 + VALIDATING 版本 + PENDING Job；返回 (job_id, version_id)。"""
    with session_scope(factory) as session:
        assets = AssetService(session)
        asset = assets.create_asset(
            name="Worker 处理器测试", asset_type=AssetType.RASTER, source=AssetSource.UPLOAD
        )
        version = assets.create_version(
            asset_id=asset.id,
            original_file_name="fixture.tif",
            size_bytes=16,
            status=AssetVersionStatus.VALIDATING,
        )
        job = Job(
            id=uuid4(),
            job_type=JobType.RASTER_INGESTION,
            status=JobStatus.PENDING,
            payload={
                "asset_version_id": str(version.id),
                "source_object_key": "uploads/fixture/src.tif",
                "source_size_bytes": 16,
            },
            asset_version_id=version.id,
            max_attempts=max_attempts,
        )
        session.add(job)
        return job.id, version.id


def test_needs_input_from_validating_version_lands_everything(
    factory: sessionmaker[Session],
) -> None:
    """A2.5 断链回归：VALIDATING 版本必须能随 Job 一起落为 NEEDS_INPUT。"""
    job_id, version_id = _prepare(factory)
    tasks._execute_ingestion(
        _FORBID_RETRY_SELF,
        str(job_id),
        _NeedsInputRunner,
        "栅格",  # type: ignore[arg-type]
    )

    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        version = session.get(AssetVersion, version_id)
        assert job is not None
        assert version is not None
        assert job.status is JobStatus.NEEDS_INPUT
        assert job.last_error is not None
        assert job.last_error["code"] == "MISSING_CRS"
        assert version.status is AssetVersionStatus.NEEDS_INPUT
        assert version.diagnostics is not None
        assert version.diagnostics["reason"] == "MISSING_CRS"
        assert version.diagnostics["missing"] == ["crs"]
        event_types = set(
            session.scalars(sa.select(JobEvent.event_type).where(JobEvent.job_id == job_id))
        )
        assert "JOB_CLAIMED" in event_types
        assert "JOB_NEEDS_INPUT" in event_types
        # NEEDS_INPUT 是暂停不是重试：不产生新的投递事件
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 0


def test_resume_after_needs_input_requeues_via_outbox(
    factory: sessionmaker[Session],
) -> None:
    """补 CRS 后 resume：版本回到 PROCESSING，Job 重新入队并生成投递事件。"""
    job_id, version_id = _prepare(factory)
    tasks._execute_ingestion(
        _FORBID_RETRY_SELF,
        str(job_id),
        _NeedsInputRunner,
        "栅格",  # type: ignore[arg-type]
    )

    with session_scope(factory) as session:
        version = session.get(AssetVersion, version_id)
        assert version is not None
        AssetService(session).resume_from_needs_input(version, user_crs="EPSG:4326")

    with session_scope(factory) as session:
        version = session.get(AssetVersion, version_id)
        job = session.get(Job, job_id)
        assert version is not None
        assert job is not None
        assert version.status is AssetVersionStatus.PROCESSING
        assert job.status is JobStatus.QUEUED
        event = session.scalars(sa.select(OutboxEvent)).first()
        assert event is not None
        assert event.status is OutboxStatus.PENDING
        assert event.payload["job_id"] == str(job_id)


def test_transient_failure_enqueues_outbox_retry(factory: sessionmaker[Session]) -> None:
    """瞬时错误：RETRYING 与重投事件同事务落库，绝不调用 Celery self.retry。"""
    job_id, version_id = _prepare(factory)
    tasks._execute_ingestion(
        _FORBID_RETRY_SELF,
        str(job_id),
        _TransientRunner,
        "栅格",  # type: ignore[arg-type]
    )

    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        version = session.get(AssetVersion, version_id)
        assert job is not None
        assert version is not None
        assert job.status is JobStatus.RETRYING
        assert job.last_error == {
            "code": "TRANSIENT",
            "detail": "MinIO 暂时不可达",
            "transient": True,
        }
        # 瞬时错误不得把版本置成终态
        assert version.status is AssetVersionStatus.VALIDATING
        events = list(session.scalars(sa.select(OutboxEvent)))
        assert len(events) == 1
        assert events[0].event_type == "job.dispatch"
        assert events[0].payload["job_id"] == str(job_id)
        assert events[0].next_attempt_at is not None


def test_transient_retry_exhaustion_fails_job_and_version(
    factory: sessionmaker[Session],
) -> None:
    job_id, version_id = _prepare(factory, max_attempts=1)
    tasks._execute_ingestion(
        _FORBID_RETRY_SELF,
        str(job_id),
        _TransientRunner,
        "栅格",  # type: ignore[arg-type]
    )

    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        version = session.get(AssetVersion, version_id)
        assert job is not None
        assert version is not None
        assert job.status is JobStatus.FAILED
        assert job.last_error is not None
        assert job.last_error["code"] == "TRANSIENT_EXHAUSTED"
        assert version.status is AssetVersionStatus.FAILED
        assert version.diagnostics is not None
        assert version.diagnostics["reason"] == "TRANSIENT_EXHAUSTED"
        # 耗尽后不再重投
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 0


def test_deterministic_failure_fails_job_and_version(factory: sessionmaker[Session]) -> None:
    job_id, version_id = _prepare(factory)
    tasks._execute_ingestion(
        _FORBID_RETRY_SELF,
        str(job_id),
        _DeterministicRunner,
        "栅格",  # type: ignore[arg-type]
    )

    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        version = session.get(AssetVersion, version_id)
        assert job is not None
        assert version is not None
        assert job.status is JobStatus.FAILED
        assert job.last_error is not None
        assert job.last_error["code"] == "UNSUPPORTED_FORMAT"
        assert version.status is AssetVersionStatus.FAILED
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 0


def test_success_path_completes(factory: sessionmaker[Session]) -> None:
    """成功路径：流水线最后一步落 READY/SUCCEEDED，心跳包装不产生副作用。"""
    job_id, version_id = _prepare(factory)
    tasks._execute_ingestion(
        _FORBID_RETRY_SELF,
        str(job_id),
        _SuccessRunner,
        "栅格",  # type: ignore[arg-type]
    )

    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        version = session.get(AssetVersion, version_id)
        assert job is not None
        assert version is not None
        assert job.status is JobStatus.SUCCEEDED
        assert version.status is AssetVersionStatus.READY
