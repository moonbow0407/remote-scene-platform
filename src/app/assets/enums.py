"""资产领域枚举。

分类不变量：栅格/矢量/附件是“物理类型”（扩展表），上传/卫星采集/外部导入是“来源”
（字段），业务分类走目录与标签（Stage 4）——不为来源或业务分类建资产表。
"""

from enum import StrEnum


class AssetType(StrEnum):
    RASTER = "RASTER"
    VECTOR = "VECTOR"
    ATTACHMENT = "ATTACHMENT"


class AssetSource(StrEnum):
    UPLOAD = "UPLOAD"
    SATELLITE = "SATELLITE"
    EXTERNAL_IMPORT = "EXTERNAL_IMPORT"


class AssetVersionStatus(StrEnum):
    UPLOADING = "UPLOADING"
    VALIDATING = "VALIDATING"
    PROCESSING = "PROCESSING"
    NEEDS_INPUT = "NEEDS_INPUT"
    READY = "READY"
    FAILED = "FAILED"
    DELETED = "DELETED"


class ArtifactKind(StrEnum):
    ORIGINAL = "ORIGINAL"
    COG = "COG"
    THUMBNAIL = "THUMBNAIL"


class ObjectCleanupKind(StrEnum):
    """对象清理类型：共享原件与版本独占工件采用不同引用判定。"""

    BLOB = "BLOB"
    ARTIFACT = "ARTIFACT"


class ObjectCleanupStatus(StrEnum):
    """跨 PostgreSQL/MinIO 清理任务状态。"""

    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
