"""矢量属性 JSONB 归一化：date/datetime/Decimal/tuple 可写，bytes 明确拒绝。

回归背景：DBF 的 D 字段读出 datetime.date、GPKG 可含 BLOB 列，原样写入
PostgreSQL JSONB 会触发序列化错误导致整个矢量 Job 失败。
"""

from __future__ import annotations

import zipfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from shapely.geometry import Point

from app.processing.detect import DetectedKind
from app.processing.errors import DeterministicError
from app.processing.vector_read import (
    normalize_json_value,
    normalize_properties,
    read_vector_layer,
)


def test_json_native_values_pass_through() -> None:
    props = {"name": "a", "count": 3, "ratio": 1.5, "flag": True, "note": None}
    assert normalize_properties(props) == props


def test_date_datetime_decimal_tuple_are_converted() -> None:
    assert normalize_json_value(date(2026, 8, 29)) == "2026-08-29"
    assert normalize_json_value(datetime(2026, 8, 29, 12, 30, 0)) == "2026-08-29T12:30:00"
    assert normalize_json_value(Decimal("1.5")) == 1.5
    assert normalize_json_value(Decimal("NaN")) == "NaN"
    assert normalize_json_value((1, "a", None)) == [1, "a", None]
    assert normalize_json_value({"a": (date(2026, 1, 1), 2)}) == {"a": ["2026-01-01", 2]}


def test_bytes_and_memoryview_are_rejected() -> None:
    with pytest.raises(DeterministicError) as exc_info:
        normalize_json_value(b"\x00\x01")
    assert exc_info.value.code == "UNSUPPORTED_PROPERTY_TYPE"

    with pytest.raises(DeterministicError):
        normalize_json_value(memoryview(b"\x00"))


def _write_shapefile_zip(path: Path) -> None:
    """生成含 D 日期字段的单点 Shapefile ZIP（无 .prj，CRS 由 user_crs 补充）。"""
    import shapefile

    base = path / "pts"
    base.mkdir(parents=True, exist_ok=True)
    writer = shapefile.Writer(str(base / "pts"), shapeType=shapefile.POINT)
    writer.field("svy_date", "D", size=8)  # pyright: ignore[reportArgumentType]
    writer.point(114.0, 30.0)
    writer.record(date(2026, 8, 29))
    writer.close()
    with zipfile.ZipFile(path / "pts.zip", "w") as archive:
        for suffix in (".shp", ".shx", ".dbf"):
            archive.write(base / f"pts{suffix}", f"pts{suffix}")


def test_shapefile_date_field_is_normalized_to_iso8601(tmp_path: Path) -> None:
    _write_shapefile_zip(tmp_path)
    layer = read_vector_layer(
        tmp_path / "pts.zip", DetectedKind.SHAPEFILE_ZIP, user_crs="EPSG:4326"
    )
    geom, props = layer.features[0]
    assert isinstance(geom, Point)
    assert props["svy_date"] == "2026-08-29"
    assert isinstance(props["svy_date"], str)
