"""矢量要素检索 API 模型。"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.pagination import MAX_PAGE_SIZE


class FeatureSearchRequest(BaseModel):
    geometry: dict[str, Any] = Field(description="EPSG:4326 GeoJSON Polygon/MultiPolygon")
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)


class FeatureItem(BaseModel):
    id: UUID
    asset_version_id: UUID
    geometry: dict[str, Any]
    properties: dict[str, Any]
