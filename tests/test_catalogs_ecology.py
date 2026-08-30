"""Stage 4A：catalogs / ecology 管理类 CRUD 行为测试。

使用内存 SQLite + create_all（仅本模块表），不触碰正式 Alembic 迁移链。
"""

from __future__ import annotations

from collections.abc import Iterator
from sqlite3 import Connection as SqliteConnection
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import Table

from app.api.app import create_app
from app.assets.enums import AssetSource, AssetType
from app.assets.models import AssetVersion, DataAsset, ObjectBlob
from app.catalogs.enums import CatalogStatus
from app.catalogs.models import ResourceCatalog, Satellite, Sensor
from app.catalogs.schemas import (
    ResourceCatalogCreate,
    ResourceCatalogUpdate,
    SatelliteCreate,
    SensorCreate,
    SensorUpdate,
)
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
from app.pagination import PageParams


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(_type: JSONB, compiler: object, **_kw: object) -> str:
    return "JSON"


def _make_engine() -> sa.Engine:
    # StaticPool + check_same_thread=False：TestClient 线程池与建表共享同一 :memory: 连接
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn: SqliteConnection, _connection_record: object) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    # 通过 metadata 取 Table，避免 Declarative __table__ 被 stub 成 FromClause
    tables: list[Table] = [
        Base.metadata.tables[ResourceCatalog.__tablename__],
        Base.metadata.tables[Satellite.__tablename__],
        Base.metadata.tables[Sensor.__tablename__],
        Base.metadata.tables[ObjectBlob.__tablename__],
        Base.metadata.tables[AssetVersion.__tablename__],
        Base.metadata.tables[DataAsset.__tablename__],
        Base.metadata.tables[EcologicalParameter.__tablename__],
        Base.metadata.tables[EcologicalParameterResourceMapping.__tablename__],
    ]
    Base.metadata.create_all(engine, tables=tables)
    return engine


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    engine = _make_engine()
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield session_factory
    engine.dispose()


@pytest.fixture()
def client(factory: sessionmaker[Session]) -> Iterator[TestClient]:
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as test_client:
        # lifespan 会写入真实 session_factory；进入上下文后再覆盖为内存库
        app.state.session_factory = factory
        yield test_client


# ---------- Resource Catalog ----------


def test_resource_create_and_tree(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        svc = CatalogService(session)
        root = svc.create_resource(ResourceCatalogCreate(code="eco", name="生态"))
        mid = svc.create_resource(
            ResourceCatalogCreate(code="eco-mine", name="矿山", parent_id=root.id)
        )
        leaf = svc.create_resource(
            ResourceCatalogCreate(code="eco-mine-a", name="矿山A", parent_id=mid.id)
        )
        tree = svc.resource_tree()
        assert len(tree) == 1
        assert tree[0].code == "eco"
        assert tree[0].children[0].code == "eco-mine"
        assert tree[0].children[0].children[0].id == leaf.id


def test_resource_duplicate_code_conflict(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        svc = CatalogService(session)
        svc.create_resource(ResourceCatalogCreate(code="dup", name="一"))
        with pytest.raises(ProblemError) as exc_info:
            svc.create_resource(ResourceCatalogCreate(code="dup", name="二"))
        assert exc_info.value.status == 409
        assert exc_info.value.code == "RESOURCE_CATALOG_CODE_CONFLICT"


def test_resource_parent_not_found(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        svc = CatalogService(session)
        with pytest.raises(ProblemError) as exc_info:
            svc.create_resource(
                ResourceCatalogCreate(code="x", name="x", parent_id=uuid4())
            )
        assert exc_info.value.status == 404


def test_resource_self_parent_rejected(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        svc = CatalogService(session)
        row = svc.create_resource(ResourceCatalogCreate(code="self", name="self"))
        with pytest.raises(ProblemError) as exc_info:
            svc.update_resource(
                row.id, ResourceCatalogUpdate(parent_id=row.id)
            )
        assert exc_info.value.status == 422


def test_resource_parent_cycle_rejected(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        svc = CatalogService(session)
        a = svc.create_resource(ResourceCatalogCreate(code="A", name="A"))
        b = svc.create_resource(ResourceCatalogCreate(code="B", name="B", parent_id=a.id))
        c = svc.create_resource(ResourceCatalogCreate(code="C", name="C", parent_id=b.id))
        with pytest.raises(ProblemError) as exc_info:
            svc.update_resource(a.id, ResourceCatalogUpdate(parent_id=c.id))
        assert exc_info.value.status == 409
        assert exc_info.value.code == "RESOURCE_CATALOG_PARENT_CYCLE"


def test_resource_delete_with_children_forbidden(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        svc = CatalogService(session)
        parent = svc.create_resource(ResourceCatalogCreate(code="p", name="p"))
        svc.create_resource(ResourceCatalogCreate(code="c", name="c", parent_id=parent.id))
        with pytest.raises(ProblemError) as exc_info:
            svc.delete_resource(parent.id)
        assert exc_info.value.code == "RESOURCE_CATALOG_HAS_CHILDREN"


# ---------- Satellite / Sensor ----------


def test_satellite_sensor_crud(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        svc = CatalogService(session)
        sat = svc.create_satellite(SatelliteCreate(code="GF1", name="高分一号"))
        sensor = svc.create_sensor(
            SensorCreate(code="GF1-PMS", name="PMS", satellite_id=sat.id)
        )
        assert sensor.satellite_id == sat.id
        page = svc.list_sensors(PageParams(page=1, page_size=20), satellite_id=sat.id)
        assert page.total == 1
        assert page.items[0].id == sensor.id
        svc.delete_sensor(sensor.id)
        svc.delete_satellite(sat.id)


def test_sensor_requires_existing_satellite(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        svc = CatalogService(session)
        with pytest.raises(ProblemError) as exc_info:
            svc.create_sensor(
                SensorCreate(code="S1", name="s", satellite_id=uuid4())
            )
        assert exc_info.value.status == 404


def test_satellite_sensor_code_unique(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        svc = CatalogService(session)
        svc.create_satellite(SatelliteCreate(code="SATA", name="A"))
        with pytest.raises(ProblemError) as exc_info:
            svc.create_satellite(SatelliteCreate(code="SATA", name="B"))
        assert exc_info.value.code == "SATELLITE_CODE_CONFLICT"

        sat = svc.create_satellite(SatelliteCreate(code="SATB", name="B"))
        svc.create_sensor(SensorCreate(code="SEN1", name="s1", satellite_id=sat.id))
        with pytest.raises(ProblemError) as exc_info:
            svc.create_sensor(SensorCreate(code="SEN1", name="s2", satellite_id=sat.id))
        assert exc_info.value.code == "SENSOR_CODE_CONFLICT"


def test_delete_satellite_with_sensors_forbidden(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        svc = CatalogService(session)
        sat = svc.create_satellite(SatelliteCreate(code="KEEP", name="keep"))
        svc.create_sensor(SensorCreate(code="KEEP-S", name="s", satellite_id=sat.id))
        with pytest.raises(ProblemError) as exc_info:
            svc.delete_satellite(sat.id)
        assert exc_info.value.code == "SATELLITE_HAS_SENSORS"


# ---------- Ecology ----------


def test_parameter_crud_and_code_unique(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        svc = EcologyService(session)
        root = svc.create_parameter(EcologicalParameterCreate(code="NDVI", name="归一化植被指数"))
        child = svc.create_parameter(
            EcologicalParameterCreate(code="NDVI-A", name="分区A", parent_id=root.id)
        )
        assert child.parent_id == root.id
        with pytest.raises(ProblemError) as exc_info:
            svc.create_parameter(EcologicalParameterCreate(code="NDVI", name="重复"))
        assert exc_info.value.code == "ECOLOGICAL_PARAMETER_CODE_CONFLICT"
        tree = svc.parameter_tree()
        assert tree[0].code == "NDVI"
        assert tree[0].children[0].code == "NDVI-A"


def test_mapping_create_and_idempotent_duplicate(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        catalogs = CatalogService(session)
        ecology = EcologyService(session)
        resource = catalogs.create_resource(ResourceCatalogCreate(code="R1", name="资源1"))
        param = ecology.create_parameter(EcologicalParameterCreate(code="P1", name="参数1"))
        first = ecology.create_mapping(
            MappingCreate(
                ecological_parameter_id=param.id, resource_catalog_id=resource.id
            )
        )
        second = ecology.create_mapping(
            MappingCreate(
                ecological_parameter_id=param.id, resource_catalog_id=resource.id
            )
        )
        assert first.id == second.id


def test_mapping_invalid_refs_fail(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        catalogs = CatalogService(session)
        ecology = EcologyService(session)
        resource = catalogs.create_resource(ResourceCatalogCreate(code="R2", name="资源2"))
        param = ecology.create_parameter(EcologicalParameterCreate(code="P2", name="参数2"))
        with pytest.raises(ProblemError) as exc_info:
            ecology.create_mapping(
                MappingCreate(
                    ecological_parameter_id=uuid4(), resource_catalog_id=resource.id
                )
            )
        assert exc_info.value.status == 404
        with pytest.raises(ProblemError) as exc_info:
            ecology.create_mapping(
                MappingCreate(
                    ecological_parameter_id=param.id, resource_catalog_id=uuid4()
                )
            )
        assert exc_info.value.status == 404


def test_batch_mapping_dedup_and_empty(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        catalogs = CatalogService(session)
        ecology = EcologyService(session)
        resource = catalogs.create_resource(ResourceCatalogCreate(code="R3", name="资源3"))
        param = ecology.create_parameter(EcologicalParameterCreate(code="P3", name="参数3"))
        empty = ecology.create_mappings_batch(MappingBatchCreate(items=[]))
        assert empty.created_count == 0
        assert empty.existing_count == 0

        item = MappingCreate(
            ecological_parameter_id=param.id, resource_catalog_id=resource.id
        )
        batch = ecology.create_mappings_batch(MappingBatchCreate(items=[item, item, item]))
        assert batch.created_count == 1
        assert batch.existing_count == 0

        again = ecology.create_mappings_batch(MappingBatchCreate(items=[item]))
        assert again.created_count == 0
        assert again.existing_count == 1
        assert again.existing[0].id == batch.created[0].id


def test_mapping_update_atomically_replaces_foreign_keys(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        catalogs = CatalogService(session)
        ecology = EcologyService(session)
        first = catalogs.create_resource(ResourceCatalogCreate(code="RU1", name="资源一"))
        second = catalogs.create_resource(ResourceCatalogCreate(code="RU2", name="资源二"))
        param = ecology.create_parameter(EcologicalParameterCreate(code="PU", name="参数"))
        mapping = ecology.create_mapping(
            MappingCreate(ecological_parameter_id=param.id, resource_catalog_id=first.id)
        )
        updated = ecology.update_mapping(
            mapping.id,
            MappingCreate(ecological_parameter_id=param.id, resource_catalog_id=second.id),
        )
        assert updated.id == mapping.id
        assert updated.resource_catalog_id == second.id


def test_delete_resource_with_mapping_forbidden(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        catalogs = CatalogService(session)
        ecology = EcologyService(session)
        resource = catalogs.create_resource(ResourceCatalogCreate(code="R4", name="资源4"))
        param = ecology.create_parameter(EcologicalParameterCreate(code="P4", name="参数4"))
        ecology.create_mapping(
            MappingCreate(
                ecological_parameter_id=param.id, resource_catalog_id=resource.id
            )
        )
        with pytest.raises(ProblemError) as exc_info:
            catalogs.delete_resource(resource.id)
        assert exc_info.value.code == "RESOURCE_CATALOG_IN_USE"

        with pytest.raises(ProblemError) as exc_info:
            ecology.delete_parameter(param.id)
        assert exc_info.value.code == "ECOLOGICAL_PARAMETER_IN_USE"


def test_delete_mapping_then_resource_ok(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        catalogs = CatalogService(session)
        ecology = EcologyService(session)
        resource = catalogs.create_resource(ResourceCatalogCreate(code="R5", name="资源5"))
        param = ecology.create_parameter(EcologicalParameterCreate(code="P5", name="参数5"))
        mapping = ecology.create_mapping(
            MappingCreate(
                ecological_parameter_id=param.id, resource_catalog_id=resource.id
            )
        )
        ecology.delete_mapping(mapping.id)
        catalogs.delete_resource(resource.id)
        ecology.delete_parameter(param.id)


# ---------- API 适配 ----------


def test_api_resource_tree_and_conflict(client: TestClient) -> None:
    r1 = client.post("/api/v1/catalogs/resources", json={"code": "root", "name": "根"})
    assert r1.status_code == 201, r1.text
    root_id = r1.json()["id"]
    r2 = client.post(
        "/api/v1/catalogs/resources",
        json={"code": "child", "name": "子", "parent_id": root_id},
    )
    assert r2.status_code == 201, r2.text
    dup = client.post("/api/v1/catalogs/resources", json={"code": "root", "name": "重复"})
    assert dup.status_code == 409
    assert dup.headers["content-type"].startswith("application/problem+json")
    tree = client.get("/api/v1/catalogs/resources/tree")
    assert tree.status_code == 200
    body = tree.json()
    assert body[0]["code"] == "root"
    assert body[0]["children"][0]["code"] == "child"


def test_api_satellite_sensor_and_ecology_mapping(client: TestClient) -> None:
    sat = client.post("/api/v1/catalogs/satellites", json={"code": "GF2", "name": "高分二号"})
    assert sat.status_code == 201, sat.text
    sat_id = sat.json()["id"]
    sensor = client.post(
        "/api/v1/catalogs/sensors",
        json={"code": "GF2-MSS", "name": "MSS", "satellite_id": sat_id},
    )
    assert sensor.status_code == 201, sensor.text
    listed = client.get(f"/api/v1/catalogs/satellites/{sat_id}/sensors")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    res = client.post("/api/v1/catalogs/resources", json={"code": "src", "name": "源"})
    param = client.post("/api/v1/ecology/parameters", json={"code": "LAI", "name": "叶面积"})
    assert res.status_code == 201 and param.status_code == 201
    mapping = client.post(
        "/api/v1/ecology/mappings",
        json={
            "ecological_parameter_id": param.json()["id"],
            "resource_catalog_id": res.json()["id"],
        },
    )
    assert mapping.status_code == 201, mapping.text
    batch = client.post(
        "/api/v1/ecology/mappings/batch",
        json={
            "items": [
                {
                    "ecological_parameter_id": param.json()["id"],
                    "resource_catalog_id": res.json()["id"],
                }
            ]
        },
    )
    assert batch.status_code == 200
    assert batch.json()["created_count"] == 0
    assert batch.json()["existing_count"] == 1


def test_disabled_status_filter(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        svc = CatalogService(session)
        svc.create_resource(
            ResourceCatalogCreate(code="on", name="启用", status=CatalogStatus.ACTIVE)
        )
        svc.create_resource(
            ResourceCatalogCreate(
                code="off", name="停用", status=CatalogStatus.DISABLED
            )
        )
        page = svc.list_resources(PageParams(), status=CatalogStatus.DISABLED)
        assert page.total == 1
        assert page.items[0].code == "off"


# ---------- Sensor 所属卫星变更保护 / 子树环检测防御 ----------


def _add_asset_for_sensor(session: Session, sensor_id: UUID, satellite_id: UUID) -> None:
    session.add(
        DataAsset(
            id=uuid4(),
            name="引用资产",
            asset_type=AssetType.RASTER,
            source=AssetSource.UPLOAD,
            satellite_id=satellite_id,
            sensor_id=sensor_id,
        )
    )
    session.flush()


def test_sensor_satellite_change_blocked_when_referenced_by_asset(
    factory: sessionmaker[Session],
) -> None:
    """传感器已被资产引用时，变更所属卫星会造成资产目录关系自相矛盾，必须 409。"""
    with session_scope(factory) as session:
        svc = CatalogService(session)
        sat_a = svc.create_satellite(SatelliteCreate(code="SAT-A", name="A"))
        sat_b = svc.create_satellite(SatelliteCreate(code="SAT-B", name="B"))
        sensor = svc.create_sensor(SensorCreate(code="SEN", name="s", satellite_id=sat_a.id))
        _add_asset_for_sensor(session, sensor.id, sat_a.id)

        with pytest.raises(ProblemError) as exc_info:
            svc.update_sensor(sensor.id, SensorUpdate(satellite_id=sat_b.id))
        assert exc_info.value.status == 409
        assert exc_info.value.code == "SENSOR_IN_USE"
        session.refresh(sensor)
        assert sensor.satellite_id == sat_a.id


def test_sensor_satellite_change_allowed_when_not_referenced(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        svc = CatalogService(session)
        sat_a = svc.create_satellite(SatelliteCreate(code="SAT-C", name="C"))
        sat_b = svc.create_satellite(SatelliteCreate(code="SAT-D", name="D"))
        sensor = svc.create_sensor(SensorCreate(code="SEN2", name="s2", satellite_id=sat_a.id))
        updated = svc.update_sensor(sensor.id, SensorUpdate(satellite_id=sat_b.id))
        assert updated.satellite_id == sat_b.id


def test_subtree_ids_detects_corrupt_cycle(factory: sessionmaker[Session]) -> None:
    """目录数据被并发改父破坏成环时，subtree_ids 必须报错终止，不得无限循环。"""
    with session_scope(factory) as session:
        svc = CatalogService(session)
        a = svc.create_resource(ResourceCatalogCreate(code="CA", name="A"))
        b = svc.create_resource(ResourceCatalogCreate(code="CB", name="B", parent_id=a.id))
        # 绕过服务层环检测，直接构造 A→B、B→A 的矛盾数据（模拟并发改父落库后果）
        session.execute(
            sa.update(ResourceCatalog).where(ResourceCatalog.id == a.id).values(parent_id=b.id)
        )
        session.flush()
        with pytest.raises(RuntimeError, match="环"):
            svc.subtree_ids(a.id)
