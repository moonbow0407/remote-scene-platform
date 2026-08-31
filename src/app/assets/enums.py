"""资产领域枚举。

栅格/矢量/附件是物理类型；业务分类走平铺 category 表。不再为来源或卫星建资产表。
"""

from enum import StrEnum


class AssetType(StrEnum):
    """物理类型：RASTER 栅格、VECTOR 矢量、ATTACHMENT 附件。"""

    RASTER = "RASTER"
    VECTOR = "VECTOR"
    ATTACHMENT = "ATTACHMENT"


class AssetStatus(StrEnum):
    """资产状态。

    UPLOADING 上传中、VALIDATING 校验中、PROCESSING 处理中、
    NEEDS_INPUT 待补元数据、READY 可用、FAILED 失败。
    软删除用 deleted_at，不另占状态值。
    """

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
