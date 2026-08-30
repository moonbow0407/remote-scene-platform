"""栅格地理参考检查回归：无 GeoTransform 时仅凭 CRS 不得产生 footprint。

回归背景：X3 旧夹具无 CRS 且无 GeoTransform，用户补 CRS 后 inspect 直接采用，
_step_footprint 把像素范围 0~128 当成真实经纬度，产出空间位置错误但 READY 的数据。
现在：无可用 GeoTransform 一律 MISSING_GEOLOCATION 阻塞；X3 夹具改为
"有 GeoTransform、无 CRS"，使"补 CRS → 断点恢复"链路可测（A2.5）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import rasterio
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.assets.enums import AssetVersionStatus
from app.assets.models import AssetVersion, RasterAssetVersion
from app.assets.service import AssetService
from app.db import Base, session_scope
from app.processing.common import IngestionContext
from app.processing.errors import NeedsInputError
from app.processing.raster_ingestion import RasterIngestion
from app.settings import Settings
from app.uploads.minio import MinioAdapter


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(_type: JSONB, compiler: object, **_kw: object) -> str:
    return "JSON"


_TABLES = (
    "data_asset",
    "asset_version",
    "object_blob",
    "asset_artifact",
    "raster_asset_version",
    "job",
    "job_event",
)


def _sqlite_after_create_without_spatialite(table: Any, bind: Any, **_kw: object) -> None:
    """替代 geoalchemy2 的 sqlite after_create 钩子。

    原钩子对 Geometry 列调用 SpatiaLite 的 RecoverGeometryColumn，纯 SQLite 无此函数；
    测试只读写非空间列，这里仅恢复 before_create 暂存的原始列定义。
    """
    table.columns = table.info.pop("_saved_columns")
    for column in table.columns:
        actual_type = getattr(column, "_actual_type", None)
        if actual_type is not None:
            column.type = actual_type
            del column._actual_type


@pytest.fixture()
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
    from geoalchemy2.admin import dialects as ga_dialects

    monkeypatch.setattr(
        ga_dialects.sqlite,  # pyright: ignore[reportPrivateImportUsage]
        "after_create",
        _sqlite_after_create_without_spatialite,
    )
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _stub_spatialite(dbapi_conn: Any, _record: object) -> None:
        # geoalchemy2 对 sqlite 读写 Geometry 列使用 SpatiaLite 函数；测试只存取 NULL，
        # 用同名桩函数透传值即可
        for name in ("AsEWKB", "GeomFromEWKB", "GeomFromEWKT"):
            dbapi_conn.create_function(name, 1, lambda value: value)

    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in _TABLES])
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


class _ForbiddenMinio:
    """源文件已就位于临时目录时，inspect 不应再访问 MinIO。"""

    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"测试不应访问 MinIO 方法：{name}")


def _prepare_version(factory: sessionmaker[Session], source_path: Path) -> UUID:
    version_id = uuid4()
    with session_scope(factory) as session:
        session.add(
            AssetVersion(
                id=version_id,
                asset_id=uuid4(),
                version_number=1,
                status=AssetVersionStatus.VALIDATING,
                original_file_name=source_path.name,
                size_bytes=source_path.stat().st_size,
                properties={},
            )
        )
    return version_id


def _make_ctx(version_id: UUID, source_path: Path, job_dir: Path) -> IngestionContext:
    ctx = IngestionContext(
        job_id=uuid4(),
        version_id=version_id,
        source_object_key=f"uploads/{uuid4()}/{source_path.name}",
        source_size_bytes=source_path.stat().st_size,
        tmp_dir=job_dir,
    )
    ctx.tmp_dir.mkdir(parents=True, exist_ok=True)
    source_path_bytes = source_path.read_bytes()
    ctx.source_path.write_bytes(source_path_bytes)
    return ctx


def _write_tif(path: Path, *, crs: str | None, transform: object | None) -> None:
    import numpy as np

    data = np.zeros((1, 8, 8), dtype="uint8")
    kwargs: dict[str, Any] = {}
    if crs is not None:
        kwargs["crs"] = crs
    if transform is not None:
        kwargs["transform"] = transform
    with rasterio.open(
        path, "w", driver="GTiff", height=8, width=8, count=1, dtype="uint8", **kwargs
    ) as dst:
        dst.write(data)


def test_inspect_blocks_without_geotransform_even_with_user_crs(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """无 GeoTransform：即使用户已补 CRS 也必须保持 NEEDS_INPUT，不得伪造空间位置。"""
    source = tmp_path / "no_georef.tif"
    _write_tif(source, crs=None, transform=None)
    version_id = _prepare_version(factory, source)
    with session_scope(factory) as session:
        AssetService(session).upsert_raster_ext(version_id, user_crs="EPSG:4326")
    ctx = _make_ctx(version_id, source, tmp_path / "job-a")

    ingestion = RasterIngestion(
        settings=cast(Settings, object()),
        minio=cast(MinioAdapter, _ForbiddenMinio()),
        engine=factory,  # type: ignore[arg-type]
    )
    with pytest.raises(NeedsInputError) as exc_info:
        ingestion._step_inspect(ctx)
    assert exc_info.value.reason == "MISSING_GEOLOCATION"


def test_inspect_requests_crs_for_georeferenced_file_without_crs(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    source = tmp_path / "no_crs.tif"
    from rasterio.transform import from_origin

    _write_tif(source, crs=None, transform=from_origin(114.0, 31.0, 0.004, 0.004))
    version_id = _prepare_version(factory, source)
    ctx = _make_ctx(version_id, source, tmp_path / "job-a")
    ingestion = RasterIngestion(
        settings=cast(Settings, object()),
        minio=cast(MinioAdapter, _ForbiddenMinio()),
        engine=factory,  # type: ignore[arg-type]
    )
    with pytest.raises(NeedsInputError) as exc_info:
        ingestion._step_inspect(ctx)
    assert exc_info.value.reason == "MISSING_CRS"


def test_inspect_recovers_with_user_crs_from_x3_style_source(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """X3 链路：有 GeoTransform 无 CRS 的源，补充 CRS 后 inspect 通过并落扩展元数据。"""
    source = tmp_path / "no_crs.tif"
    from rasterio.transform import from_origin

    _write_tif(source, crs=None, transform=from_origin(114.0, 31.0, 0.004, 0.004))
    version_id = _prepare_version(factory, source)
    ctx = _make_ctx(version_id, source, tmp_path / "job-a")
    ingestion = RasterIngestion(
        settings=cast(Settings, object()),
        minio=cast(MinioAdapter, _ForbiddenMinio()),
        engine=factory,  # type: ignore[arg-type]
    )
    with pytest.raises(NeedsInputError):
        ingestion._step_inspect(ctx)

    with session_scope(factory) as session:
        AssetService(session).upsert_raster_ext(version_id, user_crs="EPSG:4326")
    ingestion._step_inspect(ctx)

    with session_scope(factory) as session:
        ext = session.get(RasterAssetVersion, version_id)
        version = session.get(AssetVersion, version_id)
        assert ext is not None
        assert version is not None
        assert ext.crs == "EPSG:4326"
        assert ext.resolution_x is not None
        assert float(ext.resolution_x) == pytest.approx(0.004)
        assert version.status is AssetVersionStatus.PROCESSING


def test_inspect_passes_for_georeferenced_source_with_crs(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    source = tmp_path / "georef.tif"
    from rasterio.transform import from_origin

    _write_tif(source, crs="EPSG:4326", transform=from_origin(114.0, 31.0, 0.004, 0.004))
    version_id = _prepare_version(factory, source)
    ctx = _make_ctx(version_id, source, tmp_path / "job-a")
    ingestion = RasterIngestion(
        settings=cast(Settings, object()),
        minio=cast(MinioAdapter, _ForbiddenMinio()),
        engine=factory,  # type: ignore[arg-type]
    )
    ingestion._step_inspect(ctx)
    with session_scope(factory) as session:
        ext = session.get(RasterAssetVersion, version_id)
        assert ext is not None
        assert ext.crs == "EPSG:4326"
