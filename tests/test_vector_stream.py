"""矢量读取必须按要素流式进行，禁止整文件/全记录进内存。"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from shapely.geometry import Point

from app.processing.detect import DetectedKind
from app.processing.errors import DeterministicError
from app.processing.vector_ingestion import _import_projected_features
from app.processing.vector_read import read_vector_layer


def test_geojson_fixture_is_iterable() -> None:
    layer = read_vector_layer(
        Path("tests/fixtures/points.geojson"), DetectedKind.GEOJSON, user_crs=None
    )
    first = next(iter(layer.features))
    assert first[0].geom_type == "Point"
    assert "name" in first[1]


def test_geojson_read_does_not_call_json_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "pts.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [114.0, 30.0]},
                        "properties": {"name": "a"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("矢量读取不得 json.loads 整份 GeoJSON")

    monkeypatch.setattr(json, "loads", boom)
    layer = read_vector_layer(path, DetectedKind.GEOJSON, user_crs=None)
    geom, props = next(iter(layer.features))
    assert geom.geom_type == "Point"
    assert props["name"] == "a"


def test_shapefile_zip_extract_does_not_read_member_into_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shapefile

    base = tmp_path / "pts"
    base.mkdir()
    writer = shapefile.Writer(str(base / "pts"), shapeType=shapefile.POINT)
    writer.field("name", "C", size=8)  # pyright: ignore[reportArgumentType]
    writer.point(114.0, 30.0)
    writer.record("a")
    writer.close()
    archive_path = tmp_path / "pts.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for suffix in (".shp", ".shx", ".dbf"):
            archive.write(base / f"pts{suffix}", f"pts{suffix}")

    def boom(self: zipfile.ZipFile, *args: object, **kwargs: object) -> bytes:
        raise AssertionError("ZipFile.read 会把整个成员载入内存，应使用 ZipFile.open 流式复制")

    monkeypatch.setattr(zipfile.ZipFile, "read", boom)
    original_iter = shapefile.Reader.iterShapeRecords
    original_all = shapefile.Reader.shapeRecords
    calls = {"iter": 0, "all": 0}

    def wrap_iter(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls["iter"] += 1
        return original_iter(self, *args, **kwargs)

    def wrap_all(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls["all"] += 1
        return original_all(self, *args, **kwargs)

    monkeypatch.setattr(shapefile.Reader, "iterShapeRecords", wrap_iter)
    monkeypatch.setattr(shapefile.Reader, "shapeRecords", wrap_all)

    layer = read_vector_layer(archive_path, DetectedKind.SHAPEFILE_ZIP, user_crs="EPSG:4326")
    geom, props = next(iter(layer.features))
    assert geom.geom_type == "Point"
    assert props["name"] == "a"
    assert calls["all"] == 0
    assert calls["iter"] == 1


def test_geopackage_fixture_is_iterable() -> None:
    path = Path("tests/fixtures/lines.gpkg")
    if not path.is_file():
        pytest.skip("尚未生成 tests/fixtures/lines.gpkg")
    layer = read_vector_layer(path, DetectedKind.GEOPACKAGE, user_crs=None)
    geom, _props = next(iter(layer.features))
    assert geom.geom_type in {"LineString", "MultiLineString", "Geometry"}


def test_import_projected_features_flushes_in_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.processing.vector_ingestion._FEATURE_INSERT_BATCH_SIZE",
        2,
    )
    inserted: list[int] = []

    class _Dummy:
        def insert_feature_batch(self, rows: list[object]) -> None:
            inserted.append(len(rows))

    features = [(Point(float(i), 0.0), {"i": i}) for i in range(5)]
    imported, bounds, schema, geometry_type = _import_projected_features(
        _Dummy(),  # type: ignore[arg-type]
        uuid4(),
        iter(features),
        lambda geom: geom,
    )
    assert imported == 5
    assert inserted == [2, 2, 1]
    assert geometry_type == "Point"
    assert bounds[0] == 0.0
    assert schema[0]["name"] == "i"


def test_import_empty_features_is_deterministic() -> None:
    class _Dummy:
        def insert_feature_batch(self, rows: list[object]) -> None:
            raise AssertionError("空图层不应插入")

    with pytest.raises(DeterministicError) as exc:
        _import_projected_features(
            _Dummy(),  # type: ignore[arg-type]
            uuid4(),
            iter(()),
            lambda geom: geom,
        )
    assert exc.value.code == "NO_FEATURES"
