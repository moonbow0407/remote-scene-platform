"""矿山 API 请求与响应模型。"""

from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class MineFields(BaseModel):
    """参考项目矿山实体的可编辑业务字段。"""

    mine_name: str = Field(min_length=1, max_length=255, description="矿山名称")
    mine_type: int | None = Field(default=None, description="矿山类型编码")
    mine_province: str | None = Field(default=None, max_length=255, description="省份")
    mine_market: str | None = Field(default=None, max_length=255, description="市/地区")
    mine_county: str | None = Field(default=None, max_length=255, description="区县")
    mine_elevation_lower: int | None = Field(default=None, description="最低海拔，单位米")
    mine_elevation_upper: int | None = Field(default=None, description="最高海拔，单位米")
    mine_status: int | None = Field(default=None, description="矿山状态编码")
    primary_contact_name: str | None = Field(
        default=None, max_length=255, description="主要联系人"
    )
    primary_contact_phone: str | None = Field(
        default=None, max_length=255, description="主要联系人电话"
    )
    dispatch_office_phone: str | None = Field(
        default=None, max_length=255, description="调度办公室电话"
    )
    green_mine_level: str | None = Field(
        default=None, max_length=255, description="绿色矿山等级"
    )
    reclamation_rate: float | None = Field(default=None, description="复垦率")
    ecological_quality: float | None = Field(default=None, description="生态质量")

    @field_validator(
        "mine_name",
        "mine_province",
        "mine_market",
        "mine_county",
        "primary_contact_name",
        "primary_contact_phone",
        "dispatch_office_phone",
        "green_mine_level",
    )
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("文本字段不能为空字符串")
        return value

    @field_validator("mine_elevation_upper")
    @classmethod
    def _elevation_range(cls, value: int | None, info: Any) -> int | None:
        lower = info.data.get("mine_elevation_lower")
        if value is not None and lower is not None and value < lower:
            raise ValueError("最高海拔不能小于最低海拔")
        return value


class MineCreate(MineFields):
    model_config = ConfigDict(title="创建矿山", populate_by_name=True)

    mine_id: str = Field(min_length=1, max_length=255, description="矿山编号（参考系统 mine_id）")
    spatial_geojson: dict[str, Any] = Field(
        validation_alias=AliasChoices("spatial_geojson", "boundary_polygon", "geometry"),
        description="矿区空间覆盖范围：EPSG:4326 GeoJSON Polygon 或 MultiPolygon",
    )

    @field_validator("mine_id")
    @classmethod
    def _mine_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("mine_id 不能为空")
        return value


class MineUpdate(BaseModel):
    model_config = ConfigDict(title="更新矿山", populate_by_name=True)

    mine_name: str | None = Field(
        default=None, min_length=1, max_length=255, description="矿山名称"
    )
    mine_type: int | None = Field(default=None, description="矿山类型编码")
    mine_province: str | None = Field(default=None, max_length=255, description="省份")
    mine_market: str | None = Field(default=None, max_length=255, description="市/地区")
    mine_county: str | None = Field(default=None, max_length=255, description="区县")
    mine_elevation_lower: int | None = Field(default=None, description="最低海拔，单位米")
    mine_elevation_upper: int | None = Field(default=None, description="最高海拔，单位米")
    mine_status: int | None = Field(default=None, description="矿山状态编码")
    primary_contact_name: str | None = Field(
        default=None, max_length=255, description="主要联系人"
    )
    primary_contact_phone: str | None = Field(
        default=None, max_length=255, description="主要联系人电话"
    )
    dispatch_office_phone: str | None = Field(
        default=None, max_length=255, description="调度办公室电话"
    )
    green_mine_level: str | None = Field(
        default=None, max_length=255, description="绿色矿山等级"
    )
    reclamation_rate: float | None = Field(default=None, description="复垦率")
    ecological_quality: float | None = Field(default=None, description="生态质量")
    spatial_geojson: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("spatial_geojson", "boundary_polygon", "geometry"),
        description="替换矿区空间覆盖范围；EPSG:4326 GeoJSON Polygon 或 MultiPolygon",
    )

    @field_validator(
        "mine_name",
        "mine_province",
        "mine_market",
        "mine_county",
        "primary_contact_name",
        "primary_contact_phone",
        "dispatch_office_phone",
        "green_mine_level",
    )
    @classmethod
    def _strip_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("文本字段不能为空字符串")
        return value

    @field_validator("mine_elevation_upper")
    @classmethod
    def _elevation_range(cls, value: int | None, info: Any) -> int | None:
        lower = info.data.get("mine_elevation_lower")
        if value is not None and lower is not None and value < lower:
            raise ValueError("最高海拔不能小于最低海拔")
        return value


class MineResponse(MineFields):
    model_config = ConfigDict(title="矿山")

    mine_id: str = Field(description="矿山编号")
    spatial_geojson: dict[str, Any] = Field(description="EPSG:4326 GeoJSON 矿区范围")
    create_time: datetime
    update_time: datetime
