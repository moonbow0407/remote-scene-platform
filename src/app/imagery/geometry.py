"""GeoJSON 几何校验与 WKT 转换（纯函数，便于单元测试）。

约定：空间请求只接受 EPSG:4326 的 GeoJSON Polygon 或 MultiPolygon；
坐标为 [经度, 纬度] 二元组，非法输入抛 GeometryValidationError（API 层映射 422）。
"""

import math
from typing import Any


class GeometryValidationError(ValueError):
    """空间几何输入不合法。"""


_ALLOWED_TYPES = ("Polygon", "MultiPolygon")


def _validate_ring(ring: list[Any], where: str) -> list[tuple[float, float]]:
    if not isinstance(ring, list) or len(ring) < 4:
        raise GeometryValidationError(f"{where} 环至少需要 4 个坐标点")
    points: list[tuple[float, float]] = []
    for i, coord in enumerate(ring):
        if not isinstance(coord, list) or len(coord) != 2:
            raise GeometryValidationError(f"{where} 第 {i} 个坐标必须是 [经度, 纬度]")
        x, y = coord[0], coord[1]
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise GeometryValidationError(f"{where} 第 {i} 个坐标必须是数值")
        if isinstance(x, bool) or isinstance(y, bool):
            raise GeometryValidationError(f"{where} 第 {i} 个坐标必须是数值")
        longitude, latitude = float(x), float(y)
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            raise GeometryValidationError(f"{where} 第 {i} 个坐标必须是有限数值")
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise GeometryValidationError(
                f"{where} 第 {i} 个坐标超出 EPSG:4326 范围：经度 [-180, 180]、纬度 [-90, 90]"
            )
        points.append((longitude, latitude))
    if points[0] != points[-1]:
        raise GeometryValidationError(f"{where} 环必须首尾闭合")
    return points


def geojson_to_wkt(geometry: dict[str, Any]) -> str:
    """把合法的 GeoJSON Polygon/MultiPolygon 转换为 WKT 文本（SRID 由调用方声明 4326）。"""
    if not isinstance(geometry, dict):
        raise GeometryValidationError("空间几何必须是 GeoJSON 对象")
    geom_type = geometry.get("type")
    if geom_type not in _ALLOWED_TYPES:
        raise GeometryValidationError(f"仅支持 {'/'.join(_ALLOWED_TYPES)} 类型，收到 {geom_type!r}")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        raise GeometryValidationError("coordinates 不能为空")

    if geom_type == "Polygon":
        rings = [_validate_ring(ring, "Polygon") for ring in coordinates]
        return (
            "POLYGON("
            + ", ".join("(" + ", ".join(f"{x!r} {y!r}" for x, y in ring) + ")" for ring in rings)
            + ")"
        )

    polygons: list[str] = []
    for poly_idx, poly in enumerate(coordinates):
        if not isinstance(poly, list) or not poly:
            raise GeometryValidationError(f"MultiPolygon 第 {poly_idx} 个多边形坐标不合法")
        rings = [_validate_ring(ring, f"MultiPolygon[{poly_idx}]") for ring in poly]
        polygons.append(
            "("
            + ", ".join("(" + ", ".join(f"{x!r} {y!r}" for x, y in ring) + ")" for ring in rings)
            + ")"
        )
    return "MULTIPOLYGON(" + ", ".join(polygons) + ")"
