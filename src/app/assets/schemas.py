"""资产 API 模型。"""

from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from app.assets.enums import AssetStatus, AssetType
from app.pagination import MAX_PAGE_SIZE


class BBox(BaseModel):
    min_x: float = Field(description="最小经度（EPSG:4326）")
    min_y: float = Field(description="最小纬度（EPSG:4326）")
    max_x: float = Field(description="最大经度（EPSG:4326）")
    max_y: float = Field(description="最大纬度（EPSG:4326）")


class AssetListItem(BaseModel):
    id: int = Field(description="资产 ID")
    name: str = Field(description="显示名称，默认来自文件名")
    asset_type: AssetType = Field(description="物理类型")
    status: AssetStatus = Field(description="处理状态")
    category_id: int | None = Field(description="分类 ID")
    category_name: str | None = Field(default=None, description="分类名称")
    original_file_name: str = Field(description="原始文件名")
    size_bytes: int = Field(description="原文件字节数")
    acquired_at: datetime | None = Field(description="采集时间（UTC，带时区）")
    created_at: datetime = Field(description="创建时间（UTC，带时区）")
    deleted_at: datetime | None = Field(default=None, description="软删除时间；未删除为空")


class AssetDetailResponse(AssetListItem):
    diagnostics: dict[str, Any] | None = Field(description="失败或待补信息时的诊断")
    crs: str | None = Field(description="坐标系，如 EPSG:32650")
    user_crs: str | None = Field(description="用户补充的 CRS")
    width: int | None = Field(description="像素宽度")
    height: int | None = Field(description="像素高度")
    band_count: int | None = Field(description="波段数")
    bbox: BBox | None = Field(description="覆盖范围外包矩形")
    spatial_geojson: dict[str, Any] | None = Field(description="覆盖范围 GeoJSON")
    geometry_type: str | None = Field(description="矢量几何类型")
    feature_count: int | None = Field(description="导入要素数")
    mime_type: str | None = Field(description="附件 MIME")
    has_map: bool = Field(description="是否可签发地图瓦片")
    has_download: bool = Field(description="是否可下载原件")


class AssetUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255, description="新名称")
    category_id: int | None = Field(
        default=None, description="分类：省略不改；数字改为该分类；null 清除"
    )
    acquired_at: datetime | None = Field(
        default=None, description="采集时间，必须带时区；省略不改；null 清除"
    )

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name:
            raise ValueError("name 不能为空")
        return name

    @field_validator("acquired_at")
    @classmethod
    def _acquired_at_tz(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("acquired_at 必须携带时区")
        return value


class SearchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    spatial_geojson: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("spatial_geojson", "geometry"),
        description="空间范围，EPSG:4326 GeoJSON Polygon 或 MultiPolygon",
    )
    asset_type: AssetType | None = Field(default=None, description="按物理类型过滤")
    status: AssetStatus | None = Field(default=None, description="按状态过滤，常用 READY")
    acquired_from: datetime | None = Field(default=None, description="采集时间下界（含）")
    acquired_to: datetime | None = Field(default=None, description="采集时间上界（含）")
    category_id: int | None = Field(default=None, description="按分类精确过滤")
    ecological_parameter_ids: list[int] = Field(
        default_factory=list, description="经生态映射命中分类；空表示不加此过滤"
    )
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE)

    @field_validator("acquired_from", "acquired_to")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("时间必须携带时区")
        return value


class SearchItem(BaseModel):
    id: int = Field(description="资产 ID")
    name: str = Field(description="资产名称")
    asset_type: AssetType
    status: AssetStatus
    category_id: int | None = None
    category_name: str | None = None
    acquired_at: datetime | None = None
    bbox: BBox | None = None


class SubmitInputRequest(BaseModel):
    crs: str = Field(min_length=1, max_length=128, description="EPSG 代码，如 EPSG:4326")


class SubmitInputResponse(BaseModel):
    id: int = Field(description="资产 ID")
    status: AssetStatus = Field(description="提交后的状态，成功续跑后为 PROCESSING")


class DownloadUrlResponse(BaseModel):
    url: str = Field(description="短期签名下载地址")
    expires_in_seconds: int
