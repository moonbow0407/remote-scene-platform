"""卫星 / 无人机 / 统一检索接口。"""

from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from app.ecology.enums import Precision
from app.imagery.enums import RecordKind, RecordStatus
from app.pagination import MAX_PAGE_SIZE


class BBox(BaseModel):
    model_config = ConfigDict(title="经纬度范围")

    min_x: float = Field(description="西边经度，-180～180")
    min_y: float = Field(description="南边纬度，-90～90")
    max_x: float = Field(description="东边经度，-180～180")
    max_y: float = Field(description="北边纬度，-90～90")


class RecordListItem(BaseModel):
    model_config = ConfigDict(title="影像列表项")

    id: int = Field(description="记录编号")
    kind: RecordKind = Field(description="SATELLITE 或 UAV")
    name: str = Field(description="显示名称，默认等于文件名")
    data_source_id: int = Field(description="产品型号编号")
    data_source_code: str | None = Field(default=None, description="产品型号，例如 000114")
    data_source_name: str | None = Field(default=None, description="产品型号名称")
    status: RecordStatus = Field(description="处理状态。上传完成后轮询这个字段")
    original_file_name: str = Field(description="上传时的原始文件名")
    size_bytes: int = Field(description="原文件大小，单位字节")
    acquired_at: datetime | None = Field(description="数据采集时间，UTC 且带时区；未知为空")
    created_at: datetime = Field(description="记录创建时间，UTC 且带时区")
    deleted_at: datetime | None = Field(default=None, description="进入回收站的时间；未删除为空")


class RecordDetailResponse(RecordListItem):
    model_config = ConfigDict(title="影像详情")

    diagnostics: dict[str, Any] | None = Field(
        description="失败或缺少坐标系时的说明；正常处理中或已就绪为空"
    )
    crs: str | None = Field(description="文件自带的坐标系；读不到为空")
    user_crs: str | None = Field(description="人工补充的坐标系；没有补充过为空")
    width: int | None = Field(description="栅格像素宽度；未处理完为空")
    height: int | None = Field(description="栅格像素高度；未处理完为空")
    band_count: int | None = Field(description="栅格波段数；未处理完为空")
    bbox: BBox | None = Field(description="覆盖范围外包矩形；还没有空间信息时为空")
    spatial_geojson: dict[str, Any] | None = Field(
        description="覆盖范围（经纬度 GeoJSON）；还没有空间信息时为空"
    )
    has_map: bool = Field(description="true 时可以申请地图地址")
    has_download: bool = Field(description="true 时可以申请原件下载地址")


class RecordUpdateRequest(BaseModel):
    model_config = ConfigDict(title="更新影像")

    name: str | None = Field(default=None, min_length=1, max_length=255, description="新显示名称")
    acquired_at: datetime | None = Field(
        default=None, description="采集时间，必须带时区；不传则不改，传 null 表示清空"
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
    model_config = ConfigDict(
        title="影像检索条件",
        populate_by_name=True,
        json_schema_extra={"example": {"page": 1, "page_size": 20}},
    )

    spatial_geojson: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("spatial_geojson", "geometry"),
        description="检索范围。经纬度 GeoJSON 的 Polygon 或 MultiPolygon；不传则不按空间过滤",
    )
    kind: RecordKind | None = Field(
        default=None, description="只搜卫星或只搜无人机；不传则两者都搜"
    )
    status: RecordStatus | None = Field(
        default=None, description="按处理状态过滤；地图选数通常传 READY"
    )
    acquired_from: datetime | None = Field(
        default=None, description="采集时间下限（含），UTC 且带时区"
    )
    acquired_to: datetime | None = Field(
        default=None, description="采集时间上限（含），UTC 且带时区"
    )
    data_source_id: int | None = Field(default=None, description="按产品型号精确过滤；不传则不限")
    ecological_parameter_ids: list[int] = Field(
        default_factory=list,
        description="按生态细项找到对应产品型号再过滤。空数组表示不加这条条件",
    )
    precision: Precision | None = Field(
        default=None, description="按细项检索时必填：00 低精度 / 01 高精度"
    )
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(
        default=20, ge=1, le=MAX_PAGE_SIZE, description=f"每页条数，最大 {MAX_PAGE_SIZE}"
    )

    @field_validator("acquired_from", "acquired_to")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("时间必须携带时区")
        return value


class SearchItem(BaseModel):
    model_config = ConfigDict(title="影像检索结果")

    kind: RecordKind = Field(description="SATELLITE 或 UAV")
    id: int = Field(description="记录编号")
    name: str = Field(description="显示名称")
    data_source_id: int = Field(description="产品型号编号")
    data_source_code: str | None = Field(description="产品型号")
    data_source_name: str | None = Field(description="产品型号名称")
    status: RecordStatus = Field(description="处理状态")
    acquired_at: datetime | None = Field(description="数据采集时间；未知为空")
    bbox: BBox | None = Field(description="覆盖范围外包矩形；未按空间检索时通常为空")


class SubmitInputRequest(BaseModel):
    model_config = ConfigDict(title="补充坐标系")

    crs: str = Field(
        min_length=1,
        max_length=128,
        description="坐标系代码，必须写成 EPSG:数字，例如 EPSG:4326",
        examples=["EPSG:4326"],
    )


class SubmitInputResponse(BaseModel):
    model_config = ConfigDict(title="补充坐标系结果")

    kind: RecordKind
    id: int = Field(description="记录编号")
    status: RecordStatus = Field(description="提交后的处理状态，成功后续跑为 PROCESSING")


class DownloadUrlResponse(BaseModel):
    model_config = ConfigDict(title="下载地址")

    url: str = Field(description="临时下载地址，在有效期内用 GET 直接下载")
    expires_in_seconds: int = Field(description="该地址有效时间，单位秒")
