"""矢量要素检索。"""

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.pagination import MAX_PAGE_SIZE

_POLYGON_EXAMPLE = {
    "type": "Polygon",
    "coordinates": [
        [
            [116.0, 39.0],
            [117.0, 39.0],
            [117.0, 40.0],
            [116.0, 40.0],
            [116.0, 39.0],
        ]
    ],
}


class FeatureSearchRequest(BaseModel):
    """在一份矢量资产里，按多边形查出相交的要素。"""

    model_config = ConfigDict(title="要素检索条件", populate_by_name=True)

    spatial_geojson: dict[str, Any] = Field(
        validation_alias=AliasChoices("spatial_geojson", "geometry"),
        description="检索范围。必须是经纬度 GeoJSON 的 Polygon 或 MultiPolygon",
        examples=[_POLYGON_EXAMPLE],
    )
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(
        default=20, ge=1, le=MAX_PAGE_SIZE, description=f"每页条数，最大 {MAX_PAGE_SIZE}"
    )


class FeatureItem(BaseModel):
    """一条矢量要素。"""

    model_config = ConfigDict(title="矢量要素")

    id: int = Field(description="要素编号")
    asset_id: int = Field(description="所属资产编号")
    spatial_geojson: dict[str, Any] = Field(description="几何，经纬度 GeoJSON")
    properties: dict[str, Any] = Field(description="随文件带入的属性，键值对象")
