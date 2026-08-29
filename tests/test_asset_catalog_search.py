"""Stage 4：资产分类外键、目录子树与生态映射检索过滤。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
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
from app.assets.models import AssetVersion, DataAsset, ObjectBlob, PropertySchema
from app.assets.service import AssetService
from app.catalogs.models import ResourceCatalog, Satellite, Sensor
from app.catalogs.schemas import ResourceCatalogCreate, SatelliteCreate, SensorCreate
from app.catalogs.service import CatalogService
from app.db import Base, session_scope
from app.ecology.models import EcologicalParameter, EcologicalParameterResourceMapping
from app.ecology.schemas import EcologicalParameterCreate, MappingCreate
from app.ecology.service import EcologyService
from app.errors import ProblemError


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(_type: JSONB, compiler: object, **_kw: object) -> str:
    return "JSON"


@dataclass(frozen=True)
class _Seed:
    root_id: UUID
    mid_id: UUID
    leaf_id: UUID
    other_id: UUID
    sat_id: UUID
    sensor_id: UUID
    param_id: UUID
    empty_param_id: UUID
    leaf_asset_id: UUID
    other_asset_id: UUID


def _make_engine() -> sa.Engine:
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

    tables: list[Table] = [
        Base.metadata.tables[ResourceCatalog.__tablename__],
        Base.metadata.tables[Satellite.__tablename__],
        Base.metadata.tables[Sensor.__tablename__],
        Base.metadata.tables[EcologicalParameter.__tablename__],
        Base.metadata.tables[EcologicalParameterResourceMapping.__tablename__],
        Base.metadata.tables[ObjectBlob.__tablename__],
        Base.metadata.tables[DataAsset.__tablename__],
        Base.metadata.tables[AssetVersion.__tablename__],
        Base.metadata.tables[PropertySchema.__tablename__],
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
        app.state.session_factory = factory
        yield test_client


def _seed_tree(session: Session) -> _Seed:
    catalogs = CatalogService(session)
    ecology = EcologyService(session)
    root = catalogs.create_resource(ResourceCatalogCreate(code="eco", name="生态"))
    mid = catalogs.create_resource(
        ResourceCatalogCreate(code="eco-mine", name="矿山", parent_id=root.id)
    )
    leaf = catalogs.create_resource(
        ResourceCatalogCreate(code="eco-mine-a", name="矿山A", parent_id=mid.id)
    )
    other = catalogs.create_resource(ResourceCatalogCreate(code="other", name="其他"))
    sat = catalogs.create_satellite(SatelliteCreate(code="GF2", name="高分二号"))
    sensor = catalogs.create_sensor(SensorCreate(code="GF2-PMS", name="PMS", satellite_id=sat.id))
    param = ecology.create_parameter(EcologicalParameterCreate(code="LAI", name="叶面积"))
    empty_param = ecology.create_parameter(EcologicalParameterCreate(code="EMPTY", name="无映射"))
    ecology.create_mapping(
        MappingCreate(ecological_parameter_id=param.id, resource_catalog_id=leaf.id)
    )
    assets = AssetService(session)
    leaf_asset = assets.create_asset(
        name="leaf-raster",
        asset_type=AssetType.RASTER,
        source=AssetSource.UPLOAD,
        resource_catalog_id=leaf.id,
        satellite_id=sat.id,
        sensor_id=sensor.id,
    )
    assets.create_version(asset_id=leaf_asset.id, original_file_name="leaf.tif", size_bytes=10)
    other_asset = assets.create_asset(
        name="other-raster",
        asset_type=AssetType.RASTER,
        source=AssetSource.UPLOAD,
        resource_catalog_id=other.id,
    )
    assets.create_version(asset_id=other_asset.id, original_file_name="other.tif", size_bytes=10)
    return _Seed(
        root_id=root.id,
        mid_id=mid.id,
        leaf_id=leaf.id,
        other_id=other.id,
        sat_id=sat.id,
        sensor_id=sensor.id,
        param_id=param.id,
        empty_param_id=empty_param.id,
        leaf_asset_id=leaf_asset.id,
        other_asset_id=other_asset.id,
    )


def test_subtree_ids_include_self_and_descendants(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        seeded = _seed_tree(session)
        catalogs = CatalogService(session)
        ids = catalogs.subtree_ids(seeded.root_id)
        assert {seeded.root_id, seeded.mid_id, seeded.leaf_id} <= set(ids)
        assert catalogs.subtree_ids(seeded.leaf_id) == [seeded.leaf_id]


def test_search_filters_by_catalog_satellite_sensor_and_ecology(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        seeded = _seed_tree(session)
        assets = AssetService(session)

        def ids(**kwargs: object) -> set[UUID]:
            rows, _total = assets.search_versions(**kwargs)  # type: ignore[arg-type]
            return {asset.id for _version, asset in rows}

        assert ids(resource_catalog_id=seeded.leaf_id) == {seeded.leaf_asset_id}
        assert ids(resource_catalog_id=seeded.mid_id) == {seeded.leaf_asset_id}
        assert ids(resource_catalog_id=seeded.root_id) == {seeded.leaf_asset_id}
        assert ids(resource_catalog_id=seeded.other_id) == {seeded.other_asset_id}
        assert ids(satellite_id=seeded.sat_id) == {seeded.leaf_asset_id}
        assert ids(sensor_id=seeded.sensor_id) == {seeded.leaf_asset_id}
        assert ids(ecological_parameter_ids=[seeded.param_id]) == {seeded.leaf_asset_id}
        assert ids(ecological_parameter_ids=[seeded.empty_param_id]) == set()
        assert ids(ecological_parameter_ids=[]) == {
            seeded.leaf_asset_id,
            seeded.other_asset_id,
        }


def test_search_unknown_filter_ids_are_not_found(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        assets = AssetService(session)
        missing = uuid4()
        with pytest.raises(ProblemError) as exc_info:
            assets.search_versions(resource_catalog_id=missing)
        assert exc_info.value.status == 404
        with pytest.raises(ProblemError) as exc_info:
            assets.search_versions(satellite_id=missing)
        assert exc_info.value.status == 404
        with pytest.raises(ProblemError) as exc_info:
            assets.search_versions(sensor_id=missing)
        assert exc_info.value.status == 404
        with pytest.raises(ProblemError) as exc_info:
            assets.search_versions(ecological_parameter_ids=[missing])
        assert exc_info.value.status == 404


def test_create_asset_infers_satellite_from_sensor_and_rejects_mismatch(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        catalogs = CatalogService(session)
        sat_a = catalogs.create_satellite(SatelliteCreate(code="A", name="A"))
        sat_b = catalogs.create_satellite(SatelliteCreate(code="B", name="B"))
        sensor = catalogs.create_sensor(SensorCreate(code="A-S", name="s", satellite_id=sat_a.id))
        assets = AssetService(session)
        inferred = assets.create_asset(
            name="inferred",
            asset_type=AssetType.RASTER,
            source=AssetSource.SATELLITE,
            sensor_id=sensor.id,
        )
        assert inferred.satellite_id == sat_a.id
        with pytest.raises(ProblemError) as exc_info:
            assets.create_asset(
                name="mismatch",
                asset_type=AssetType.RASTER,
                source=AssetSource.SATELLITE,
                satellite_id=sat_b.id,
                sensor_id=sensor.id,
            )
        assert exc_info.value.status == 422


def test_delete_resource_in_use_by_asset_forbidden(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ProblemError) as exc_info, session_scope(factory) as session:
        seeded = _seed_tree(session)
        CatalogService(session).delete_resource(seeded.other_id)
    assert exc_info.value.code == "RESOURCE_CATALOG_IN_USE"


def test_delete_satellite_with_sensors_forbidden(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ProblemError) as exc_info, session_scope(factory) as session:
        seeded = _seed_tree(session)
        CatalogService(session).delete_satellite(seeded.sat_id)
    assert exc_info.value.code == "SATELLITE_HAS_SENSORS"


def test_delete_sensor_in_use_by_asset_forbidden(factory: sessionmaker[Session]) -> None:
    with pytest.raises(ProblemError) as exc_info, session_scope(factory) as session:
        seeded = _seed_tree(session)
        CatalogService(session).delete_sensor(seeded.sensor_id)
    assert exc_info.value.code == "SENSOR_IN_USE"


def test_api_patch_and_search_filters(client: TestClient, factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        seeded = _seed_tree(session)

    leaf_id = str(seeded.leaf_id)
    root_id = str(seeded.root_id)
    other_id = str(seeded.other_id)
    sat_id = str(seeded.sat_id)
    sensor_id = str(seeded.sensor_id)
    param_id = str(seeded.param_id)
    empty_param_id = str(seeded.empty_param_id)
    leaf_asset_id = str(seeded.leaf_asset_id)
    other_asset_id = str(seeded.other_asset_id)

    detail = client.get(f"/api/v1/assets/{leaf_asset_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["resource_catalog_id"] == leaf_id
    assert body["satellite_id"] == sat_id
    assert body["sensor_id"] == sensor_id

    patched = client.patch(
        f"/api/v1/assets/{other_asset_id}",
        json={"name": "renamed", "resource_catalog_id": other_id},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "renamed"
    assert patched.json()["resource_catalog_id"] == other_id

    by_root = client.post("/api/v1/assets/search", json={"resource_catalog_id": root_id})
    assert by_root.status_code == 200, by_root.text
    assert {item["asset_id"] for item in by_root.json()["items"]} == {leaf_asset_id}

    by_sat = client.post("/api/v1/assets/search", json={"satellite_id": sat_id})
    assert {item["asset_id"] for item in by_sat.json()["items"]} == {leaf_asset_id}

    by_sensor = client.post("/api/v1/assets/search", json={"sensor_id": sensor_id})
    assert {item["asset_id"] for item in by_sensor.json()["items"]} == {leaf_asset_id}

    by_eco = client.post("/api/v1/assets/search", json={"ecological_parameter_ids": [param_id]})
    assert by_eco.status_code == 200
    assert {item["asset_id"] for item in by_eco.json()["items"]} == {leaf_asset_id}

    empty_map = client.post(
        "/api/v1/assets/search", json={"ecological_parameter_ids": [empty_param_id]}
    )
    assert empty_map.status_code == 200
    assert empty_map.json()["items"] == []
    assert empty_map.json()["total"] == 0

    unknown = client.post("/api/v1/assets/search", json={"resource_catalog_id": str(uuid4())})
    assert unknown.status_code == 404
    assert unknown.headers["content-type"].startswith("application/problem+json")
