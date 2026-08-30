"""Stage 5 监测服务行为测试（内存 SQLite，不依赖外部基础设施）。

覆盖：计划 CRUD 校验、暂停/恢复、到期派发、occurrence 唯一幂等、停机补跑、
增量窗口、输入快照不可变、Run 状态推进与派发接缝。

方言说明：SQLite 无 PostGIS，本文件的自动夹具把选择查询的空间维度置空
（boundary_wkt=None），空间相交过滤由 tests/integration/test_stage5_monitoring.py
在真实 PostgreSQL/PostGIS 上验证；SQLite 侧丢失 tzinfo 的 timestamptz 由服务
边界按 UTC 归一（与生产语义一致）。全部时间锚点使用固定 T0，不依赖真实时钟。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from sqlite3 import Connection as SqliteConnection
from uuid import UUID, uuid4

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
from app.catalogs.schemas import ResourceCatalogCreate
from app.catalogs.service import CatalogService
from app.db import Base, session_scope
from app.ecology.models import EcologicalParameter, EcologicalParameterResourceMapping
from app.ecology.schemas import (
    EcologicalParameterCreate,
    MappingBatchCreate,
    MappingCreate,
)
from app.ecology.service import EcologyService
from app.errors import ProblemError
from app.jobs.models import Job
from app.monitoring.enums import OccurrenceStatus, OccurrenceTrigger, PlanStatus, RunStatus
from app.monitoring.models import (
    MonitoringOccurrence,
    MonitoringPlan,
    MonitoringRun,
    MonitoringRunInput,
)
from app.monitoring.schemas import PlanCreate, PlanUpdate
from app.monitoring.selection import SelectionCriteria, is_in_window, select_ready_versions
from app.monitoring.service import MonitoringService, RunDispatcher
from app.pagination import PageParams

T0 = datetime(2026, 8, 30, tzinfo=UTC)

_VALID_BOUNDARY = {
    "type": "Polygon",
    "coordinates": [[[114.0, 30.0], [115.0, 30.0], [115.0, 31.0], [114.0, 31.0], [114.0, 30.0]]],
}


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(_type: JSONB, compiler: object, **_kw: object) -> str:
    return "JSON"


class RecordingDispatcher:
    """派发接缝的测试替身：记录 (run_id, 输入版本集合)，不创建 Job。"""

    def __init__(self) -> None:
        self.calls: list[tuple[UUID, list[UUID]]] = []

    def dispatch(
        self, session: Session, run: MonitoringRun, input_version_ids: list[UUID]
    ) -> UUID | None:
        self.calls.append((run.id, list(input_version_ids)))
        return None


class FailingDispatcher:
    """派发失败的测试替身：模拟 Job+Outbox 写入异常，整个事务必须回滚。"""

    def dispatch(
        self, session: Session, run: MonitoringRun, input_version_ids: list[UUID]
    ) -> UUID | None:
        raise RuntimeError("模拟派发失败")


def _make_engine() -> sa.Engine:
    # StaticPool + check_same_thread=False：TestClient 线程池与建表共享同一 :memory: 连接
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _sqlite_dialect_stubs(dbapi_conn: SqliteConnection, _record: object) -> None:
        # SpatiaLite 不可用：把 geoalchemy2 的 SQLite 管理函数与 EWKT 绑定替换为
        # 惰性桩，使 monitoring_plan.boundary 列退化为"只写文本"；业务读取走
        # boundary_wkt 列，空间相交在 PG 集成测试验证
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
        # 实体读取会把 boundary 列包进 AsEWKB(...)：返回合法的空 MULTIPOLYGON
        # EWKB（01=LE, type=6|0x20000000 带 SRID, srid=4326, num=0），构造
        # WKBElement 不再解析 WKT 文本而失败；测试不读取该列内容
        dbapi_conn.create_function("AsEWKB", 1, lambda _value: "0106000020a086010000000000")
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        # pysqlite legacy 事务模式会让 SAVEPOINT 脱离外层事务（RELEASE 即提交），
        # 按 SQLAlchemy 官方配方关闭其隐式 BEGIN，由方言接管显式 BEGIN，
        # 使 _insert_occurrence 的 SAVEPOINT 幂等语义在测试方言下与 PostgreSQL 一致
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


@pytest.fixture()
def no_spatial_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQLite 无 PostGIS：选择查询的空间维度置空，其余过滤逻辑真实执行。"""
    import app.monitoring.service as service_module
    from app.monitoring.selection import select_ready_versions as real_select

    def _without_boundary(session: Session, criteria: SelectionCriteria) -> list[AssetVersion]:
        return real_select(session, replace(criteria, boundary_wkt=None))

    monkeypatch.setattr(service_module, "select_ready_versions", _without_boundary)


@pytest.fixture(autouse=True)
def _disable_spatial_in_service_tests(no_spatial_filter: None) -> None:
    """本文件全部服务层用例统一在无空间维度下运行。"""


def _service(session: Session, dispatcher: RunDispatcher | None = None) -> MonitoringService:
    # 单元测试默认注入替身（不落 Job/Outbox）；真实派发器 JobRunDispatcher 的
    # 行为由 tests/test_monitoring_dispatch.py 与 PostgreSQL 集成测试覆盖
    return MonitoringService(session, dispatcher or RecordingDispatcher())


def _plan_body(**overrides: object) -> PlanCreate:
    values: dict[str, object] = {
        "name": "矿山监测计划",
        "boundary": _VALID_BOUNDARY,
        "schedule_type": "INTERVAL",
        "schedule_expression": "PT1H",
        "timezone": "Asia/Shanghai",
        "resource_catalog_id": None,
        "ecological_parameter_ids": [],
    }
    values.update(overrides)
    return PlanCreate(**values)  # type: ignore[arg-type]


def _create_plan(
    session: Session,
    dispatcher: RunDispatcher | None = None,
    **overrides: object,
) -> tuple[MonitoringService, MonitoringPlan]:
    service = _service(session, dispatcher)
    plan = service.create_plan(_plan_body(**overrides))
    return service, plan


def _seed_ready_version(
    session: Session,
    *,
    name: str,
    catalog_id: UUID | None = None,
    acquired_at: datetime | None = None,
    created_at: datetime | None = None,
) -> AssetVersion:
    assets = AssetService(session)
    asset = assets.create_asset(
        name=name,
        asset_type=AssetType.RASTER,
        source=AssetSource.SATELLITE,
        resource_catalog_id=catalog_id,
    )
    version = assets.create_version(
        asset_id=asset.id,
        original_file_name=f"{name}.tif",
        size_bytes=8,
        status=AssetVersionStatus.READY,
        acquired_at=acquired_at,
    )
    # 时间锚点显式固定：不依赖真实时钟（增量窗口断言需要确定的时间关系）
    version.created_at = created_at if created_at is not None else T0
    session.flush()
    return version


# ---------- 计划 CRUD ----------


def test_create_plan_computes_next_run_and_stores_boundary(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        before = datetime.now(UTC)
        service, plan = _create_plan(session)
        assert plan.status is PlanStatus.ACTIVE
        assert plan.next_run_at is not None and plan.next_run_at > before
        # Polygon 归一化为 MULTIPOLYGON 存储
        assert plan.boundary_wkt.startswith("MULTIPOLYGON")
        views = service.describe_plans([plan])
        assert views[plan.id].ecological_parameter_ids == []


def test_create_plan_rejects_invalid_boundary(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        with pytest.raises(ProblemError) as exc_info:
            _create_plan(
                session,
                boundary={"type": "Point", "coordinates": [114.0, 30.0]},
            )
        assert exc_info.value.status == 422


def test_create_plan_rejects_out_of_range_boundary(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        boundary = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [200.0, 0.0], [200.0, 10.0], [0.0, 10.0], [0.0, 0.0]]],
        }
        with pytest.raises(ProblemError):
            _create_plan(session, boundary=boundary)


def test_create_plan_rejects_missing_catalog_and_parameter(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        with pytest.raises(ProblemError) as catalog_error:
            _create_plan(session, resource_catalog_id=uuid4())
        assert catalog_error.value.status == 404
        with pytest.raises(ProblemError) as parameter_error:
            _create_plan(session, ecological_parameter_ids=[uuid4()])
        assert parameter_error.value.status == 404


def test_create_plan_rejects_invalid_schedule_and_timezone(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        with pytest.raises(ProblemError):
            _create_plan(session, schedule_type="RRULE", schedule_expression="FREQ=SECONDLY")
        with pytest.raises(ProblemError):
            _create_plan(
                session,
                schedule_type="RRULE",
                schedule_expression="FREQ=DAILY;DTSTART:20260101T000000Z",
            )
        with pytest.raises(ProblemError):
            _create_plan(session, timezone="Mars/Olympus")


def test_create_plan_dedupes_and_validates_parameters(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        parameter = EcologyService(session).create_parameter(
            EcologicalParameterCreate(code="veg", name="植被覆盖")
        )
        service, plan = _create_plan(session, ecological_parameter_ids=[parameter.id, parameter.id])
        views = service.describe_plans([plan])
        assert views[plan.id].ecological_parameter_ids == [parameter.id]


def test_update_plan_replaces_fields_and_recomputes_schedule(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        catalog = CatalogService(session).create_resource(
            ResourceCatalogCreate(code="mine", name="矿山")
        )
        parameter = EcologyService(session).create_parameter(
            EcologicalParameterCreate(code="veg", name="植被")
        )
        service, plan = _create_plan(session)
        original_next_run = plan.next_run_at
        assert original_next_run is not None

        updated = service.update_plan(
            plan.id,
            PlanUpdate(
                name="改名后的计划",
                schedule_expression="P1D",
                resource_catalog_id=catalog.id,
                ecological_parameter_ids=[parameter.id],
            ),
        )
        assert updated.name == "改名后的计划"
        assert updated.schedule_expression == "P1D"
        assert updated.resource_catalog_id == catalog.id
        # 调度变更后从当前时刻重算，晚于原 next_run_at
        assert updated.next_run_at is not None
        assert updated.next_run_at > original_next_run
        views = service.describe_plans([updated])
        assert views[updated.id].ecological_parameter_ids == [parameter.id]


def test_update_plan_clears_catalog_with_explicit_null(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        catalog = CatalogService(session).create_resource(
            ResourceCatalogCreate(code="mine", name="矿山")
        )
        service, plan = _create_plan(session, resource_catalog_id=catalog.id)
        updated = service.update_plan(plan.id, PlanUpdate(resource_catalog_id=None))
        assert updated.resource_catalog_id is None


def test_pause_and_resume_control_scheduling(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.monitoring.service as service_module

    t_pause = T0 + timedelta(hours=10)
    monkeypatch.setattr(service_module, "now_utc", lambda: t_pause)
    with session_scope(factory) as session:
        service, plan = _create_plan(session)
        plan.next_run_at = T0  # 制造已到期状态
        service.pause_plan(plan.id)
        assert plan.status is PlanStatus.PAUSED

        summary = service.process_due_plans(now=t_pause)
        assert summary.plans_considered == 0  # PAUSED 不触发
        assert summary.dispatched == 0

        resumed = service.resume_plan(plan.id)
        assert resumed.status is PlanStatus.ACTIVE
        # 恢复后从当前时刻重算下一周期，暂停期间的错过周期不补录
        assert resumed.next_run_at == T0 + timedelta(hours=11)
        again = service.process_due_plans(now=t_pause)
        assert again.dispatched == 0
        assert again.missed_recorded == 0


def test_pause_twice_conflict(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        service, plan = _create_plan(session)
        service.pause_plan(plan.id)
        with pytest.raises(ProblemError) as exc_info:
            service.pause_plan(plan.id)
        assert exc_info.value.status == 409


def test_delete_plan_removes_plan(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        service, plan = _create_plan(session)
        service.delete_plan(plan.id)
        assert service.get_plan(plan.id) is None


# ---------- 到期派发与 occurrence 幂等 ----------


def test_first_tick_dispatches_exactly_once(factory: sessionmaker[Session]) -> None:
    dispatcher = RecordingDispatcher()
    with session_scope(factory) as session:
        service, plan = _create_plan(session, dispatcher)
        plan.next_run_at = T0
        summary = service.process_due_plans(now=T0 + timedelta(minutes=5))
        assert summary.dispatched == 1
        run = session.scalar(sa.select(MonitoringRun))
        assert run is not None
        assert run.status is RunStatus.PENDING
        occurrence = session.scalar(sa.select(MonitoringOccurrence))
        assert occurrence is not None
        assert occurrence.status is OccurrenceStatus.DISPATCHED
        assert occurrence.trigger is OccurrenceTrigger.SCHEDULED
        # next_run_at 推进到下一网格点，同刻重复扫描不再派发
        next_run = plan.next_run_at
        assert next_run is not None
        assert next_run.replace(tzinfo=UTC) > T0 + timedelta(minutes=5)
        assert len(dispatcher.calls) == 1
        again = service.process_due_plans(now=T0 + timedelta(minutes=5))
        assert again.dispatched == 0
        assert len(dispatcher.calls) == 1


def test_catch_up_records_missed_and_dispatches_latest_once(
    factory: sessionmaker[Session],
) -> None:
    """停机跨 3 个周期：只补最近一次执行，其余记录 MISSED。"""
    with session_scope(factory) as session:
        service, plan = _create_plan(session)
        plan.next_run_at = T0
        summary = service.process_due_plans(now=T0 + timedelta(hours=3))
        assert summary.missed_recorded == 3
        assert summary.dispatched == 1
        missed = list(
            session.scalars(
                sa.select(MonitoringOccurrence)
                .where(MonitoringOccurrence.status == OccurrenceStatus.MISSED)
                .order_by(MonitoringOccurrence.scheduled_for)
            )
        )
        assert [row.scheduled_for.replace(tzinfo=UTC) for row in missed] == [
            T0,
            T0 + timedelta(hours=1),
            T0 + timedelta(hours=2),
        ]
        run = session.scalar(sa.select(MonitoringRun))
        assert run is not None
        assert run.status is RunStatus.PENDING
        # 不产生任务风暴：MISSED 周期没有对应 Run
        assert session.scalar(sa.select(sa.func.count()).select_from(MonitoringRun)) == 1


def test_rollback_tick_leaves_no_occurrence_then_recovers(
    factory: sessionmaker[Session],
) -> None:
    """崩溃模拟：派发事务回滚后不留下半状态，重启后同一周期正常生成。"""
    with session_scope(factory) as session:
        _, plan = _create_plan(session)
        plan.next_run_at = T0
    session = factory()
    try:
        _service(session).process_due_plans(now=T0 + timedelta(minutes=1))
        session.rollback()
        assert session.scalar(sa.select(sa.func.count()).select_from(MonitoringRun)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(MonitoringOccurrence)) == 0
    finally:
        session.rollback()
        session.close()
    with session_scope(factory) as session:
        plan = session.scalar(sa.select(MonitoringPlan))
        assert plan is not None
        plan.next_run_at = T0
        summary = _service(session, RecordingDispatcher()).process_due_plans(
            now=T0 + timedelta(minutes=1)
        )
        assert summary.dispatched == 1
        assert session.scalar(sa.select(sa.func.count()).select_from(MonitoringRun)) == 1


def test_occurrence_unique_constraint_blocks_duplicate_dispatch(
    factory: sessionmaker[Session],
) -> None:
    """同一 (plan, scheduled_for) 只能派发一次：重复扫描撞唯一约束后跳过。"""
    with session_scope(factory) as session:
        _, plan = _create_plan(session)
        plan.next_run_at = T0
        _service(session, RecordingDispatcher()).process_due_plans(now=T0)
    with session_scope(factory) as session:
        plan = session.scalar(sa.select(MonitoringPlan))
        assert plan is not None
        plan.next_run_at = T0  # 模拟 next_run_at 推进前崩溃后的重复扫描
        summary = _service(session, RecordingDispatcher()).process_due_plans(now=T0)
        assert summary.dispatched == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(MonitoringRun)) == 1
        assert session.scalar(sa.select(sa.func.count()).select_from(MonitoringOccurrence)) == 1


def test_manual_trigger_creates_run_and_duplicate_conflicts(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.monitoring.service as service_module

    monkeypatch.setattr(service_module, "now_utc", lambda: T0)
    dispatcher = RecordingDispatcher()
    with session_scope(factory) as session:
        service, plan = _create_plan(session, dispatcher)
        run = service.trigger_plan(plan.id)
        assert run.status is RunStatus.PENDING
        occurrence = session.get(MonitoringOccurrence, run.occurrence_id)
        assert occurrence is not None
        assert occurrence.trigger is OccurrenceTrigger.MANUAL
        assert len(dispatcher.calls) == 1
    with session_scope(factory) as session:
        service = _service(session, dispatcher)
        plan = session.scalar(sa.select(MonitoringPlan))
        assert plan is not None
        with pytest.raises(ProblemError) as exc_info:
            service.trigger_plan(plan.id)
        assert exc_info.value.code == "MONITORING_OCCURRENCE_DUPLICATE"


def test_dispatch_failure_rolls_back_whole_tick(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        _, plan = _create_plan(session)
        plan.next_run_at = T0
    session = factory()
    try:
        with pytest.raises(RuntimeError):
            _service(session, FailingDispatcher()).process_due_plans(now=T0)
        session.rollback()
        assert session.scalar(sa.select(sa.func.count()).select_from(MonitoringRun)) == 0
        assert session.scalar(sa.select(sa.func.count()).select_from(MonitoringOccurrence)) == 0
    finally:
        session.rollback()
        session.close()


# ---------- 增量选择 ----------


def test_is_in_window_semantics() -> None:
    anchor = T0
    # 无历史成功执行：全量
    assert is_in_window(acquired_at=None, created_at=T0 - timedelta(days=1), anchor=None)
    # 采集时间晚于锚点
    assert is_in_window(acquired_at=T0 + timedelta(hours=1), created_at=T0, anchor=anchor)
    # 采集时间早于锚点但注册时间晚于锚点（晚注册的既有采集数据）
    assert is_in_window(
        acquired_at=T0 - timedelta(days=365),
        created_at=T0 + timedelta(hours=1),
        anchor=anchor,
    )
    # 采集与注册均早于锚点：旧数据不重复选择
    assert not is_in_window(
        acquired_at=T0 - timedelta(days=1), created_at=T0 - timedelta(days=1), anchor=anchor
    )
    # 无采集时间且注册早于锚点
    assert not is_in_window(acquired_at=None, created_at=T0 - timedelta(hours=1), anchor=anchor)


def test_selection_only_ready_versions(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        kept = _seed_ready_version(session, name="kept")
        processing = _seed_ready_version(session, name="processing")
        needs_input = _seed_ready_version(session, name="needs-input")
        failed = _seed_ready_version(session, name="failed")
        processing.status = AssetVersionStatus.PROCESSING
        needs_input.status = AssetVersionStatus.NEEDS_INPUT
        failed.status = AssetVersionStatus.FAILED
        session.flush()
        criteria = SelectionCriteria(
            boundary_wkt=None,
            resource_catalog_id=None,
            ecological_parameter_ids=(),
            window_anchor=None,
        )
        selected = {version.id for version in select_ready_versions(session, criteria)}
        assert selected == {kept.id}


def test_selection_filters_by_catalog_and_ecology_mapping(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        catalogs = CatalogService(session)
        mine = catalogs.create_resource(ResourceCatalogCreate(code="mine", name="矿山"))
        other = catalogs.create_resource(ResourceCatalogCreate(code="other", name="其他"))
        ecology = EcologyService(session)
        parameter = ecology.create_parameter(EcologicalParameterCreate(code="veg", name="植被"))
        ecology.create_mappings_batch(
            MappingBatchCreate(
                items=[
                    MappingCreate(ecological_parameter_id=parameter.id, resource_catalog_id=mine.id)
                ]
            )
        )
        in_scope = _seed_ready_version(session, name="in-scope", catalog_id=mine.id)
        out_of_scope = _seed_ready_version(session, name="out-of-scope", catalog_id=other.id)
        _seed_ready_version(session, name="no-catalog")

        by_catalog = select_ready_versions(
            session,
            SelectionCriteria(
                boundary_wkt=None,
                resource_catalog_id=mine.id,
                ecological_parameter_ids=(),
                window_anchor=None,
            ),
        )
        assert [version.id for version in by_catalog] == [in_scope.id]

        by_ecology = select_ready_versions(
            session,
            SelectionCriteria(
                boundary_wkt=None,
                resource_catalog_id=None,
                ecological_parameter_ids=(parameter.id,),
                window_anchor=None,
            ),
        )
        assert [version.id for version in by_ecology] == [in_scope.id]
        assert out_of_scope.id not in {version.id for version in by_ecology}


def test_selection_incremental_window(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        assets = AssetService(session)
        asset = assets.create_asset(
            name="多版本资产", asset_type=AssetType.RASTER, source=AssetSource.SATELLITE
        )
        anchor = T0
        old_version = assets.create_version(
            asset_id=asset.id,
            original_file_name="old.tif",
            size_bytes=8,
            status=AssetVersionStatus.READY,
            acquired_at=anchor - timedelta(days=2),
        )
        old_version.created_at = anchor - timedelta(days=2)
        selected_old = assets.create_version(
            asset_id=asset.id,
            original_file_name="selected-old.tif",
            size_bytes=8,
            status=AssetVersionStatus.READY,
            acquired_at=anchor + timedelta(hours=1),
        )
        selected_old.created_at = anchor + timedelta(minutes=30)
        late_registered = assets.create_version(
            asset_id=asset.id,
            original_file_name="late.tif",
            size_bytes=8,
            status=AssetVersionStatus.READY,
            acquired_at=anchor - timedelta(days=1),
        )
        late_registered.created_at = anchor + timedelta(hours=2)
        new_version = assets.create_version(
            asset_id=asset.id,
            original_file_name="new.tif",
            size_bytes=8,
            status=AssetVersionStatus.READY,
            acquired_at=None,
        )
        new_version.created_at = anchor + timedelta(hours=3)
        session.flush()

        selected = {
            version.id
            for version in select_ready_versions(
                session,
                SelectionCriteria(
                    boundary_wkt=None,
                    resource_catalog_id=None,
                    ecological_parameter_ids=(),
                    window_anchor=anchor,
                ),
            )
        }
        assert selected == {selected_old.id, late_registered.id, new_version.id}
        assert old_version.id not in selected


# ---------- 输入快照与执行状态 ----------


def test_run_snapshot_freezes_versions_and_ignores_new_versions(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.monitoring.service as service_module

    monkeypatch.setattr(service_module, "now_utc", lambda: T0)
    dispatcher = RecordingDispatcher()
    with session_scope(factory) as session:
        v1 = _seed_ready_version(session, name="资产A")
        v2 = _seed_ready_version(session, name="资产B")
        service, plan = _create_plan(session, dispatcher)
        run = service.trigger_plan(plan.id)
        assert {row.asset_version_id for row in run.inputs} == {v1.id, v2.id}
        assert len(dispatcher.calls) == 1
        assert set(dispatcher.calls[0][1]) == {v1.id, v2.id}

    with session_scope(factory) as session:
        # Run 创建后资产新增版本：历史快照不变
        _seed_ready_version(session, name="资产A")
        run = session.scalar(sa.select(MonitoringRun))
        assert run is not None
        versions = {
            row.asset_version_id
            for row in session.scalars(
                sa.select(MonitoringRunInput).where(MonitoringRunInput.run_id == run.id)
            )
        }
        assert len(versions) == 2


def test_second_run_selects_only_new_versions(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.monitoring.service as service_module

    monkeypatch.setattr(service_module, "now_utc", lambda: T0)
    dispatcher = RecordingDispatcher()
    with session_scope(factory) as session:
        v1 = _seed_ready_version(session, name="资产A")
        service, plan = _create_plan(session, dispatcher)
        run = service.trigger_plan(plan.id)
        service.mark_run_started(run.id)
        service.mark_run_succeeded(run.id)
        # SQLite 方言回读 timestamptz 为 naive UTC，与生产语义一致故按 UTC 比对
        assert plan.last_successful_run_at is not None
        assert plan.last_successful_run_at.replace(tzinfo=UTC) == T0
        assert dispatcher.calls[0][1] == [v1.id]

    monkeypatch.setattr(service_module, "now_utc", lambda: T0 + timedelta(hours=1))
    with session_scope(factory) as session:
        # 上一次成功执行后新增版本；旧版本不再进入选择
        v2 = _seed_ready_version(session, name="资产A", created_at=T0 + timedelta(minutes=30))
        service = _service(session, dispatcher)
        plan = session.scalar(sa.select(MonitoringPlan))
        assert plan is not None
        run = service.trigger_plan(plan.id)
        inputs = {
            row.asset_version_id
            for row in session.scalars(
                sa.select(MonitoringRunInput).where(MonitoringRunInput.run_id == run.id)
            )
        }
        assert inputs == {v2.id}
        assert dispatcher.calls[-1][1] == [v2.id]


def test_failed_run_does_not_advance_window(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.monitoring.service as service_module

    monkeypatch.setattr(service_module, "now_utc", lambda: T0)
    with session_scope(factory) as session:
        v1 = _seed_ready_version(session, name="资产A")
        service, plan = _create_plan(session, RecordingDispatcher())
        run = service.trigger_plan(plan.id)
        service.mark_run_started(run.id)
        failed = service.mark_run_failed(run.id, detail="处理失败")
        assert failed.status is RunStatus.FAILED
        assert failed.diagnostics is not None
        assert plan.last_successful_run_at is None

    monkeypatch.setattr(service_module, "now_utc", lambda: T0 + timedelta(hours=1))
    with session_scope(factory) as session:
        service = _service(session, RecordingDispatcher())
        plan = session.scalar(sa.select(MonitoringPlan))
        assert plan is not None
        run = service.trigger_plan(plan.id)
        inputs = {
            row.asset_version_id
            for row in session.scalars(
                sa.select(MonitoringRunInput).where(MonitoringRunInput.run_id == run.id)
            )
        }
        # 失败执行不推进锚点：同一批数据会被下一次执行重选
        assert inputs == {v1.id}


def test_run_state_transitions_validated(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        _seed_ready_version(session, name="资产A")
        service, plan = _create_plan(session, RecordingDispatcher())
        run = service.trigger_plan(plan.id)
        with pytest.raises(ProblemError) as exc_info:
            service.mark_run_succeeded(run.id)  # PENDING 不能直接成功
        assert exc_info.value.code == "MONITORING_RUN_STATE_INVALID"
        service.mark_run_started(run.id)
        assert run.started_at is not None
        succeeded = service.mark_run_succeeded(run.id)
        assert succeeded.status is RunStatus.SUCCEEDED
        assert succeeded.finished_at is not None
        with pytest.raises(ProblemError):
            service.mark_run_started(run.id)  # 终态不可再转换


def test_list_runs_and_inputs(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        _seed_ready_version(session, name="资产A")
        service, plan = _create_plan(session, RecordingDispatcher())
        run = service.trigger_plan(plan.id)
        page = service.list_runs(plan.id, PageParams())
        assert page.total == 1
        views = service.describe_runs(page.items)
        assert views[run.id].input_count == 1
        inputs = service.list_run_inputs(run.id, PageParams())
        assert inputs.total == 1
