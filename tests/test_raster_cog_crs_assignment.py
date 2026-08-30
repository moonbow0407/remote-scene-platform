"""补 CRS 场景的 COG 生成方式回归：不得对已生成的 COG 做就地修改。

回归背景：旧实现先 driver="COG" 生成最终 COG，再用 r+ 打开指派用户 CRS；
COG 驱动只支持 CreateCopy，就地更新会破坏 COG 优化布局甚至直接失败。
现在：源无 CRS 时先把用户 CRS 指派到轻量 VRT 中间层（仅元数据），再一次性
生成最终 COG；最终 COG 一旦生成就不再修改。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import numpy as np
import pytest
import rasterio
import sqlalchemy as sa
from rasterio.transform import from_origin
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.assets.enums import ArtifactKind, AssetVersionStatus
from app.assets.models import AssetArtifact, AssetVersion
from app.assets.service import AssetService
from app.db import Base, session_scope
from app.processing.common import IngestionContext
from app.processing.raster_ingestion import RasterIngestion


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(_type: JSONB, compiler: object, **_kw: object) -> str:
    return "JSON"


_TABLES = ("asset_version", "object_blob", "asset_artifact", "raster_asset_version")


def _sqlite_after_create_without_spatialite(table: Any, bind: Any, **_kw: object) -> None:
    """与 test_raster_inspect_geolocation 相同：替代 geoalchemy2 的 SpatiaLite 钩子。"""
    table.columns = table.info.pop("_saved_columns")
    for column in table.columns:
        actual_type = getattr(column, "_actual_type", None)
        if actual_type is not None:
            column.type = actual_type
            del column._actual_type


@pytest.fixture()
def factory(monkeypatch: pytest.MonkeyPatch) -> Iterator[sessionmaker[Session]]:
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
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


class _RecordingMinio:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, Path]] = []

    def head_object(self, *, key: str) -> dict[str, object] | None:
        return None

    def upload_file(self, *, local_path: str, key: str, content_type: str) -> None:
        self.uploads.append((key, Path(local_path)))

    def download_to_file(self, *, key: str, local_path: str) -> None:
        raise AssertionError("源已就位，不应触发下载")


class _ExistingCogMinio(_RecordingMinio):
    def head_object(self, *, key: str) -> dict[str, object] | None:
        return {"size": 10, "etag": "fixture"}


def _write_tif(path: Path, *, crs: str | None) -> None:
    kwargs: dict[str, object] = {"transform": from_origin(114.0, 31.0, 0.004, 0.004)}
    if crs is not None:
        kwargs["crs"] = crs
    with rasterio.open(
        path, "w", driver="GTiff", height=16, width=16, count=1, dtype="uint8", **kwargs
    ) as dst:
        dst.write(np.zeros((1, 16, 16), dtype="uint8"))


def _prepare(factory: sessionmaker[Session], source: Path, *, user_crs: str | None) -> UUID:
    version_id = uuid4()
    with session_scope(factory) as session:
        session.add(
            AssetVersion(
                id=version_id,
                asset_id=uuid4(),
                version_number=1,
                status=AssetVersionStatus.PROCESSING,
                original_file_name=source.name,
                size_bytes=source.stat().st_size,
                properties={},
            )
        )
        if user_crs is not None:
            AssetService(session).upsert_raster_ext(version_id, user_crs=user_crs)
    return version_id


def _make_ctx(version_id: UUID, source: Path, job_dir: Path) -> IngestionContext:
    ctx = IngestionContext(
        job_id=uuid4(),
        version_id=version_id,
        source_object_key=f"uploads/{uuid4()}/{source.name}",
        source_size_bytes=source.stat().st_size,
        tmp_dir=job_dir,
    )
    ctx.tmp_dir.mkdir(parents=True, exist_ok=True)
    ctx.source_path.write_bytes(source.read_bytes())
    return ctx


def test_cog_gets_user_crs_via_staging_not_inplace_update(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """源无 CRS + 用户补 CRS：先经 VRT 指派 CRS，最终 COG 一次性生成且定位正确。"""
    source = tmp_path / "no_crs.tif"
    _write_tif(source, crs=None)
    version_id = _prepare(factory, source, user_crs="EPSG:4326")
    ctx = _make_ctx(version_id, source, tmp_path / "job-a")
    minio = _RecordingMinio()
    ingestion = RasterIngestion(settings=object(), minio=minio, engine=factory)  # type: ignore[arg-type]

    ingestion._step_create_cog(ctx)

    assert len(minio.uploads) == 1
    cog_key, cog_path = minio.uploads[0]
    assert cog_key == f"artifacts/{version_id}/cog.tif"
    with rasterio.open(cog_path) as dataset:
        assert dataset.crs is not None
        assert dataset.crs.to_epsg() == 4326
        assert dataset.transform.a == pytest.approx(0.004)
        assert dataset.transform.c == pytest.approx(114.0)
        assert dataset.transform.f == pytest.approx(31.0)
    with session_scope(factory) as session:
        artifact = session.scalars(
            sa.select(AssetArtifact).where(AssetArtifact.asset_version_id == version_id)
        ).first()
        assert artifact is not None
        assert artifact.kind is ArtifactKind.COG


def test_cog_keeps_source_crs_without_staging(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """源自带 CRS：直接一次成 COG，不需要 CRS 指派中间层。"""
    source = tmp_path / "with_crs.tif"
    _write_tif(source, crs="EPSG:4326")
    version_id = _prepare(factory, source, user_crs=None)
    ctx = _make_ctx(version_id, source, tmp_path / "job-b")
    minio = _RecordingMinio()
    ingestion = RasterIngestion(settings=object(), minio=minio, engine=factory)  # type: ignore[arg-type]

    ingestion._step_create_cog(ctx)

    assert len(minio.uploads) == 1
    with rasterio.open(minio.uploads[0][1]) as dataset:
        assert dataset.crs is not None
        assert dataset.crs.to_epsg() == 4326
    assert not ctx.staged_vrt_path.exists()


def test_cog_step_skips_when_artifact_exists(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """COG 工件已存在且对象可访问：幂等跳过，不再上传。"""
    source = tmp_path / "no_crs.tif"
    _write_tif(source, crs=None)
    version_id = _prepare(factory, source, user_crs="EPSG:4326")
    with session_scope(factory) as session:
        session.add(
            AssetArtifact(
                id=uuid4(),
                asset_version_id=version_id,
                kind=ArtifactKind.COG,
                object_key=f"artifacts/{version_id}/cog.tif",
                size_bytes=10,
                content_type="image/tiff",
            )
        )
    ctx = _make_ctx(version_id, source, tmp_path / "job-c")
    minio = _ExistingCogMinio()
    ingestion = RasterIngestion(settings=object(), minio=minio, engine=factory)  # type: ignore[arg-type]

    ingestion._step_create_cog(ctx)

    assert minio.uploads == []
