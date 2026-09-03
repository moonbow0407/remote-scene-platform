"""影像记录与清理枚举。"""

from enum import StrEnum

from app.schema_docs import enum_docs


@enum_docs("记录种类", "SATELLITE：卫星影像；UAV：无人机影像。")
class RecordKind(StrEnum):
    SATELLITE = "SATELLITE"
    UAV = "UAV"


@enum_docs(
    "处理状态",
    "UPLOADING：正在上传文件；"
    "VALIDATING：正在校验文件；"
    "PROCESSING：后台处理中，请继续查详情；"
    "NEEDS_INPUT：缺坐标系，请调用「补充坐标系」；"
    "READY：处理完成，可下载、可申请地图地址；"
    "FAILED：失败，原因见 diagnostics。",
)
class RecordStatus(StrEnum):
    UPLOADING = "UPLOADING"
    VALIDATING = "VALIDATING"
    PROCESSING = "PROCESSING"
    NEEDS_INPUT = "NEEDS_INPUT"
    READY = "READY"
    FAILED = "FAILED"


class ObjectCleanupKind(StrEnum):
    OBJECT = "OBJECT"


class ObjectCleanupStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RETRYING = "RETRYING"
    SUCCEEDED = "SUCCEEDED"
