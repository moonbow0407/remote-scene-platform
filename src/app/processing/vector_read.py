"""读取 GeoJSON / Shapefile ZIP / GeoPackage 为源 CRS 下的 shapely 几何。"""

from __future__ import annotations

import json
import math
import sqlite3
import zipfile
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from shapely import from_wkb
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from app.processing.detect import DetectedKind, _safe_zip_name
from app.processing.errors import DeterministicError, NeedsInputError
from app.processing.gpkg import decode_geometry


@dataclass
class VectorLayer:
    source_crs: str | None
    geometry_type: str
    features: list[tuple[BaseGeometry, dict[str, Any]]]


def read_vector_layer(path: Path, kind: DetectedKind, *, user_crs: str | None) -> VectorLayer:
    if kind is DetectedKind.GEOJSON:
        layer = _read_geojson(path)
    elif kind is DetectedKind.SHAPEFILE_ZIP:
        layer = _read_shapefile_zip(path)
    elif kind is DetectedKind.GEOPACKAGE:
        layer = _read_geopackage(path)
    else:
        raise DeterministicError("UNSUPPORTED_FORMAT", f"不支持的矢量格式：{kind}")
    layer = replace(
        layer,
        features=[(geom, normalize_properties(props)) for geom, props in layer.features],
    )
    if not layer.features:
        raise DeterministicError("NO_FEATURES", "矢量文件不包含可导入要素")
    if layer.source_crs is None:
        if user_crs is None:
            raise NeedsInputError(
                reason="MISSING_CRS",
                detail="矢量缺少 CRS 且未提供补充信息；请提交 EPSG 代码后从断点继续",
            )
        layer.source_crs = user_crs
    return layer


def normalize_properties(props: dict[str, Any]) -> dict[str, Any]:
    """把要素属性归一化为可写入 PostgreSQL JSONB 的 JSON 类型。"""
    return {str(key): normalize_json_value(value) for key, value in props.items()}


def normalize_json_value(value: Any) -> Any:
    """单个属性值的 JSONB 归一化。

    DBF/GPKG 常见 date/datetime/Decimal，均转换为 JSON 兼容表示；
    bytes 等无法忠实表示的类型明确拒绝——写入 JSONB 前静默修正或丢字段
    都会掩盖源数据问题。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        number = float(value)
        return number if math.isfinite(number) else str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [normalize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_json_value(item) for key, item in value.items()}
    raise DeterministicError(
        "UNSUPPORTED_PROPERTY_TYPE",
        f"矢量属性包含无法写入 JSONB 的类型 {type(value).__name__}；"
        "请转换源数据属性后重新上传",
    )


def _read_geojson(path: Path) -> VectorLayer:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("type") == "FeatureCollection":
        features_raw = payload.get("features")
    else:
        features_raw = [payload]
    if not isinstance(features_raw, list):
        raise DeterministicError("INVALID_VECTOR_ARCHIVE", "GeoJSON FeatureCollection 不合法")
    features: list[tuple[BaseGeometry, dict[str, Any]]] = []
    types: set[str] = set()
    for item in features_raw:
        if not isinstance(item, dict) or item.get("type") != "Feature":
            raise DeterministicError("INVALID_VECTOR_ARCHIVE", "GeoJSON 含非 Feature 成员")
        geom_obj = item.get("geometry")
        if not geom_obj:
            continue
        geom = shape(geom_obj)
        if geom.is_empty:
            continue
        if not geom.is_valid:
            raise DeterministicError("INVALID_GEOMETRY", "GeoJSON 含无效几何")
        raw_props = item.get("properties")
        props: dict[str, Any] = dict(raw_props) if isinstance(raw_props, dict) else {}
        features.append((geom, props))
        types.add(geom.geom_type)
    # RFC 7946：坐标为 WGS84，等同 EPSG:4326
    return VectorLayer(source_crs="EPSG:4326", geometry_type=_unify_types(types), features=features)


def _read_shapefile_zip(path: Path) -> VectorLayer:
    import shapefile

    unpack = path.parent / "unpack"
    unpack.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = _safe_zip_name(info.filename)
            target = unpack / Path(name).name
            target.write_bytes(archive.read(info))
    shp_files = list(unpack.glob("*.shp"))
    if len(shp_files) != 1:
        raise DeterministicError("INVALID_VECTOR_ARCHIVE", "ZIP 中必须恰好包含一个 .shp")
    shp_path = shp_files[0]
    reader = shapefile.Reader(str(shp_path.with_suffix("")))
    field_names = [field[0] for field in reader.fields[1:]]
    features: list[tuple[BaseGeometry, dict[str, Any]]] = []
    types: set[str] = set()
    for sr in reader.shapeRecords():
        shp = sr.shape
        rec = sr.record
        if shp is None or rec is None:
            continue
        geojson: dict[str, Any] = dict(shp.__geo_interface__)
        if geojson.get("type") is None:
            continue
        geom = shape(geojson)
        if geom.is_empty:
            continue
        if not geom.is_valid:
            raise DeterministicError("INVALID_GEOMETRY", "Shapefile 含无效几何")
        props = {
            str(field_names[i]): _normalize_shp_value(rec[i]) for i in range(len(field_names))
        }
        features.append((geom, props))
        types.add(geom.geom_type)
    reader.close()
    crs = _crs_from_prj(shp_path.with_suffix(".prj"))
    return VectorLayer(source_crs=crs, geometry_type=_unify_types(types), features=features)


def _read_geopackage(path: Path) -> VectorLayer:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        contents = conn.execute(
            "SELECT table_name, srs_id FROM gpkg_contents WHERE data_type='features'"
        ).fetchall()
        if not contents:
            raise DeterministicError("INVALID_VECTOR_ARCHIVE", "GeoPackage 没有要素表")
        if len(contents) != 1:
            raise DeterministicError(
                "INVALID_VECTOR_ARCHIVE", "首版仅支持含一个要素表的 GeoPackage"
            )
        table = str(contents[0]["table_name"])
        srs_id = contents[0]["srs_id"]
        geom_col_row = conn.execute(
            "SELECT column_name, geometry_type_name, srs_id FROM gpkg_geometry_columns "
            "WHERE table_name=?",
            (table,),
        ).fetchone()
        if geom_col_row is None:
            raise DeterministicError("INVALID_VECTOR_ARCHIVE", "缺少 gpkg_geometry_columns")
        geom_col = str(geom_col_row["column_name"])
        if srs_id is None:
            srs_id = geom_col_row["srs_id"]
        columns = [info[1] for info in conn.execute(f"PRAGMA table_info({_quote_ident(table)})")]
        attr_cols = [c for c in columns if c != geom_col]
        rows = conn.execute(f"SELECT * FROM {_quote_ident(table)}").fetchall()
    finally:
        conn.close()

    features: list[tuple[BaseGeometry, dict[str, Any]]] = []
    types: set[str] = set()
    for row in rows:
        mapping = dict(row)
        blob = mapping.get(geom_col)
        if blob is None:
            continue
        _srs, wkb = decode_geometry(bytes(blob))
        geom = from_wkb(wkb)
        if geom.is_empty:
            continue
        if not geom.is_valid:
            raise DeterministicError("INVALID_GEOMETRY", "GeoPackage 含无效几何")
        props = {col: mapping[col] for col in attr_cols if col.lower() != "fid"}
        features.append((geom, props))
        types.add(geom.geom_type)
    crs = f"EPSG:{int(srs_id)}" if srs_id not in (None, 0) else None
    return VectorLayer(source_crs=crs, geometry_type=_unify_types(types), features=features)


def _crs_from_prj(path: Path) -> str | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        return None
    from pyproj import CRS

    try:
        crs = CRS.from_user_input(text)
        epsg = crs.to_epsg()
        return f"EPSG:{epsg}" if epsg is not None else str(crs)
    except Exception:
        return None


def _unify_types(types: set[str]) -> str:
    if not types:
        return "Geometry"
    if len(types) == 1:
        return next(iter(types))
    return "Geometry"


def _normalize_shp_value(value: Any) -> Any:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    return value


def _quote_ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise DeterministicError("INVALID_VECTOR_ARCHIVE", f"非法表名：{name}")
    return f'"{name}"'


def shapely_to_wkt(geom: BaseGeometry) -> str:
    return geom.wkt
