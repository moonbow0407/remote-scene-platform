"""资产 API 模型。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.assets.enums import AssetSource, AssetType, AssetVersionStatus
from app.pagination import MAX_PAGE_SIZE


class VersionSummary(BaseModel):
    id: UUID
    version_number: int
    status: AssetVersionStatus
    original_file_name: str
    size_bytes: int
    acquired_at: datetime | None
    created_at: datetime


class AssetDetailResponse(BaseModel):
    id: UUID
    name: str
    asset_type: AssetType
    source: AssetSource
    properties: dict[str, Any]
    current_version: VersionSummary | None
    created_at: datetime


class BBox(BaseModel):
    min_x: float
    min_y: float
    max_x: float
    max_y: float


class RasterExtResponse(BaseModel):
    crs: str | None
    user_crs: str | None
    width: int | None
    height: int | None
    band_count: int | None
    bands: list[dict[str, Any]] | None
    resolution_x: float | None
    resolution_y: float | None
    nodata: float | None
    render_profile: dict[str, Any] | None
    footprint_geojson: dict[str, Any] | None = None
    bbox: BBox | None


class ArtifactResponse(BaseModel):
    id: UUID
    kind: str
    object_key: str
    size_bytes: int | None
    content_type: str | None


class VersionDetailResponse(VersionSummary):
    properties: dict[str, Any]
    diagnostics: dict[str, Any] | None
    raster: RasterExtResponse | None
    artifacts: list[ArtifactResponse]


class SearchRequest(BaseModel):
    geometry: dict[str, Any] | None = Field(
        default=None, description="EPSG:4326 GeoJSON Polygon/MultiPolygon"
    )
    asset_type: AssetType | None = None
    version_status: AssetVersionStatus | None = None
    acquired_from: datetime | None = None
    acquired_to: datetime | None = None
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE, description="每页条数")

    @field_validator("acquired_from", "acquired_to")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("时间必须携带时区，例如 2026-08-29T12:00:00+08:00")
        return value


class SearchItem(BaseModel):
    asset_id: UUID
    asset_name: str
    asset_type: AssetType
    version_id: UUID
    version_number: int
    status: AssetVersionStatus
    acquired_at: datetime | None
    bbox: BBox | None


class SubmitInputRequest(BaseModel):
    crs: str = Field(min_length=1, max_length=128, description="EPSG 代码，如 EPSG:4326")
