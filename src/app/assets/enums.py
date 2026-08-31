"""资产领域枚举。

栅格/矢量/附件是物理类型；业务分类走平铺 category 表。不再为来源或卫星建资产表。
"""

from enum import StrEnum

from app.schema_docs import enum_docs


@enum_docs(
    "文件种类",
    "RASTER：栅格影像（.tif / .tiff）；"
    "VECTOR：矢量（.zip / .geojson / .gpkg / .shp）；"
    "ATTACHMENT：其他附件。",
)
class AssetType(StrEnum):
    RASTER = "RASTER"
    VECTOR = "VECTOR"
    ATTACHMENT = "ATTACHMENT"


@enum_docs(
    "资产状态",
    "UPLOADING：正在上传文件；"
    "VALIDATING：正在校验文件；"
    "PROCESSING：后台处理中，请继续查详情；"
    "NEEDS_INPUT：缺坐标系，请调用「补充坐标系」；"
    "READY：处理完成，可下载；栅格还可申请地图地址；"
    "FAILED：失败，原因见 diagnostics。"
    "进入回收站不改这个字段，看 deleted_at。",
)
class AssetStatus(StrEnum):
    UPLOADING = "UPLOADING"
    VALIDATING = "VALIDATING"
    PROCESSING = "PROCESSING"
    NEEDS_INPUT = "NEEDS_INPUT"
    READY = "READY"
    FAILED = "FAILED"


class ObjectCleanupKind(StrEnum):
    """MinIO 对象清理。一行资产上的原件/COG/缩略图都按对象键删除。"""

    OBJECT = "OBJECT"


class ObjectCleanupStatus(StrEnum):
    """跨 PostgreSQL/MinIO 清理任务状态。"""

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
