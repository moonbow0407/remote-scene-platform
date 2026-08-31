"""矢量要素检索 API 模型。"""

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.pagination import MAX_PAGE_SIZE


class FeatureSearchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    spatial_geojson: dict[str, Any] = Field(
        validation_alias=AliasChoices("spatial_geojson", "geometry"),
        description="检索范围，EPSG:4326 GeoJSON Polygon 或 MultiPolygon",
    )
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(
        default=20, ge=1, le=MAX_PAGE_SIZE, description=f"每页条数，上限 {MAX_PAGE_SIZE}"
    )


class FeatureItem(BaseModel):
    id: int = Field(description="要素 ID")
    asset_id: int = Field(description="所属资产 ID")
    spatial_geojson: dict[str, Any] = Field(description="要素几何，EPSG:4326 GeoJSON")
    properties: dict[str, Any] = Field(description="动态属性（JSONB）")
