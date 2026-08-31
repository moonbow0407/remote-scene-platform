"""资产 API 模型。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from app.assets.enums import AssetSource, AssetType, AssetVersionStatus
from app.pagination import MAX_PAGE_SIZE


class VersionSummary(BaseModel):
    id: UUID = Field(description="资产版本 ID")
    version_number: int = Field(description="版本号，从 1 递增；历史版本不可覆盖")
    status: AssetVersionStatus = Field(
        description="版本状态：UPLOADING 上传中、VALIDATING 校验中、PROCESSING 处理中、"
        "NEEDS_INPUT 待补元数据、READY 可用、FAILED 失败、DELETED 已删除"
    )
    original_file_name: str = Field(description="原始文件名")
    size_bytes: int = Field(description="原始文件字节数")
    acquired_at: datetime | None = Field(description="数据采集时间（UTC，带时区）")
    created_at: datetime = Field(description="版本创建时间（UTC，带时区）")


class AssetDetailResponse(BaseModel):
    id: UUID = Field(description="逻辑资产 ID")
    name: str = Field(description="资产显示名称")
    asset_type: AssetType = Field(description="物理类型：RASTER 栅格、VECTOR 矢量、ATTACHMENT 附件")
    source: AssetSource = Field(
        description="来源：UPLOAD 本机上传、SATELLITE 卫星采集、EXTERNAL_IMPORT 外部导入"
    )
    resource_catalog_id: UUID | None = Field(description="业务分类：资源目录节点 ID")
    resource_catalog_code: str | None = Field(default=None, description="资源目录编码")
    resource_catalog_name: str | None = Field(default=None, description="资源目录名称")
    satellite_id: UUID | None = Field(description="卫星平台 ID")
    satellite_code: str | None = Field(default=None, description="卫星平台编码")
    satellite_name: str | None = Field(default=None, description="卫星平台名称")
    sensor_id: UUID | None = Field(description="传感器目录 ID；须属于该卫星平台")
    sensor_code: str | None = Field(default=None, description="传感器稳定编码")
    sensor_name: str | None = Field(default=None, description="传感器显示名称")
    properties: dict[str, Any] = Field(
        description="扩展业务属性（JSONB），须符合已登记的 JSON Schema"
    )
    current_version: VersionSummary | None = Field(description="当前指向的版本；尚无版本则为空")
    created_at: datetime = Field(description="资产创建时间（UTC，带时区）")


class AssetUpdateRequest(BaseModel):
    """部分更新逻辑资产；未出现字段保持不变，分类外键显式 null 表示清除。"""

    name: str | None = Field(
        default=None, min_length=1, max_length=255, description="新显示名称；省略不改"
    )
    resource_catalog_id: UUID | None = Field(
        default=None, description="资源目录：省略不改；UUID 改为该分类；null 清除分类"
    )
    satellite_id: UUID | None = Field(
        default=None, description="卫星：省略不改；UUID 改为该卫星；null 清除"
    )
    sensor_id: UUID | None = Field(
        default=None, description="传感器：省略不改；UUID 改为该传感器；null 清除"
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


class BBox(BaseModel):
    min_x: float = Field(description="最小经度（EPSG:4326）")
    min_y: float = Field(description="最小纬度（EPSG:4326）")
    max_x: float = Field(description="最大经度（EPSG:4326）")
    max_y: float = Field(description="最大纬度（EPSG:4326）")


class RasterExtResponse(BaseModel):
    crs: str | None = Field(
        description="栅格原始坐标系，如 EPSG:32650；缺失且无法推断时版本会进入 NEEDS_INPUT"
    )
    user_crs: str | None = Field(description="用户补充的 CRS；仅纠错链路使用")
    width: int | None = Field(description="像素宽度")
    height: int | None = Field(description="像素高度")
    band_count: int | None = Field(description="波段数")
    bands: list[dict[str, Any]] | None = Field(description="各波段描述（数据类型、描述等）")
    resolution_x: float | None = Field(description="X 方向地面分辨率，单位与原始 CRS 一致")
    resolution_y: float | None = Field(description="Y 方向地面分辨率，单位与原始 CRS 一致")
    nodata_value: float | None = Field(description="无效值")
    render_profile: dict[str, Any] | None = Field(description="瓦片渲染配置，含展示用波段索引")
    epsg_code: int | None = Field(default=None, description="坐标系 EPSG 代码，如 32650")
    spatial_geojson: dict[str, Any] | None = Field(
        default=None, description="覆盖范围，EPSG:4326 GeoJSON"
    )
    bbox: BBox | None = Field(description="覆盖范围外包矩形，EPSG:4326")


class VectorExtResponse(BaseModel):
    crs: str | None = Field(description="矢量原始坐标系")
    user_crs: str | None = Field(description="用户补充的 CRS")
    geometry_type: str | None = Field(description="几何类型，如 Polygon / MultiPolygon / Point")
    feature_count: int | None = Field(description="导入到 PostGIS 的要素数量")
    native_format: str | None = Field(description="原文件格式，如 geojson / shapefile / gpkg")
    property_schema: list[dict[str, Any]] | None = Field(description="动态属性字段清单")
    epsg_code: int | None = Field(default=None, description="坐标系 EPSG 代码")
    spatial_geojson: dict[str, Any] | None = Field(
        default=None, description="要素并集外包，EPSG:4326 GeoJSON"
    )
    bbox: BBox | None = Field(description="外包矩形，EPSG:4326")


class AttachmentExtResponse(BaseModel):
    mime_type: str | None = Field(description="探测到的 MIME 类型")
    detected_format: str | None = Field(description="探测到的文件格式")
    original_file_name: str | None = Field(description="原始文件名")


class PropertySchemaItem(BaseModel):
    name: str = Field(description="属性模式名称")
    asset_type: AssetType | None = Field(description="绑定的物理类型；空表示通用")
    json_schema: dict[str, Any] = Field(description="JSON Schema 正文，写入 properties 前按此校验")


class PropertySchemaUpsert(BaseModel):
    json_schema: dict[str, Any] = Field(description="JSON Schema 正文")
    asset_type: AssetType | None = Field(default=None, description="绑定物理类型；省略表示不限类型")


class ArtifactResponse(BaseModel):
    id: UUID = Field(description="工件 ID")
    kind: str = Field(description="工件种类：ORIGINAL 原文件、COG 云优化 GeoTIFF、THUMBNAIL 缩略图")
    bucket: str = Field(description="MinIO 桶")
    object_key: str = Field(description="MinIO 对象键")
    size_bytes: int | None = Field(description="字节数")
    content_type: str | None = Field(description="MIME 类型")


class VersionDetailResponse(VersionSummary):
    properties: dict[str, Any] = Field(description="该版本冻结的业务属性")
    diagnostics: dict[str, Any] | None = Field(
        description="处理诊断；NEEDS_INPUT / FAILED 时查看这里"
    )
    raster: RasterExtResponse | None = Field(description="栅格扩展；非栅格版本为空")
    vector: VectorExtResponse | None = Field(default=None, description="矢量扩展；非矢量版本为空")
    attachment: AttachmentExtResponse | None = Field(
        default=None, description="附件扩展；非附件版本为空"
    )
    artifacts: list[ArtifactResponse] = Field(description="该版本产生的不可变工件")


class SearchRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    spatial_geojson: dict[str, Any] | None = Field(
        default=None,
        validation_alias=AliasChoices("spatial_geojson", "geometry"),
        description="空间范围，EPSG:4326 GeoJSON Polygon 或 MultiPolygon；省略则不按空间过滤",
    )
    asset_type: AssetType | None = Field(
        default=None, description="按物理类型过滤：RASTER / VECTOR / ATTACHMENT"
    )
    version_status: AssetVersionStatus | None = Field(
        default=None, description="按版本状态过滤，常用 READY"
    )
    acquired_from: datetime | None = Field(
        default=None, description="采集时间下界（含），必须带时区"
    )
    acquired_to: datetime | None = Field(default=None, description="采集时间上界（含），必须带时区")
    resource_catalog_id: UUID | None = Field(
        default=None, description="资源目录节点 ID；命中该节点及其全部子树"
    )
    satellite_id: UUID | None = Field(default=None, description="按卫星过滤")
    sensor_id: UUID | None = Field(default=None, description="按传感器过滤")
    ecological_parameter_ids: list[UUID] = Field(
        default_factory=list,
        description=(
            "生态参数 ID 列表。空列表表示不加此过滤；有值则经映射命中资源目录。映射为空时返回空结果"
        ),
    )
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(default=20, ge=1, le=MAX_PAGE_SIZE, description="每页条数")

    @field_validator("acquired_from", "acquired_to")
    @classmethod
    def _require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("时间必须携带时区，例如 2026-08-29T12:00:00+08:00")
        return value


class SearchItem(BaseModel):
    asset_id: UUID = Field(description="逻辑资产 ID")
    asset_name: str = Field(description="资产名称")
    asset_type: AssetType = Field(description="物理类型")
    version_id: UUID = Field(description="命中的资产版本 ID")
    version_number: int = Field(description="版本号")
    status: AssetVersionStatus = Field(description="该版本状态")
    acquired_at: datetime | None = Field(description="数据采集时间（UTC，带时区）")
    resource_catalog_id: UUID | None = Field(default=None, description="资源目录节点 ID")
    resource_catalog_code: str | None = Field(default=None, description="资源目录编码")
    resource_catalog_name: str | None = Field(default=None, description="资源目录名称")
    satellite_id: UUID | None = Field(default=None, description="卫星平台 ID")
    satellite_code: str | None = Field(default=None, description="卫星平台编码")
    satellite_name: str | None = Field(default=None, description="卫星平台名称")
    sensor_id: UUID | None = Field(default=None, description="传感器目录 ID")
    sensor_code: str | None = Field(default=None, description="传感器编码")
    sensor_name: str | None = Field(default=None, description="传感器名称")
    bbox: BBox | None = Field(description="覆盖范围外包矩形；未做空间过滤时可能为空")


class SubmitInputRequest(BaseModel):
    crs: str = Field(
        min_length=1, max_length=128, description="EPSG 代码，如 EPSG:4326，用于从 NEEDS_INPUT 续跑"
    )


class SubmitInputResponse(BaseModel):
    asset_version_id: str = Field(description="资产版本 ID")
    status: str = Field(description="提交后的版本状态，成功续跑后为 PROCESSING")


class ArtifactDownloadUrlResponse(BaseModel):
    url: str = Field(description="短期签名下载地址，过期后需重新申请")
    expires_in_seconds: int = Field(description="签名有效期，单位秒")
    kind: str = Field(description="工件种类：ORIGINAL / COG / THUMBNAIL")
