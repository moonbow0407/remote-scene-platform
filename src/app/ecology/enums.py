"""生态模块枚举。"""

from enum import StrEnum

from app.schema_docs import enum_docs


@enum_docs("启用状态", "ACTIVE：启用；DISABLED：停用。")
class EcologicalParameterStatus(StrEnum):
    """生态参数是否启用。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


@enum_docs("反演精度", "00：低精度；01：高精度。")
class Precision(StrEnum):
    LOW = "00"
    HIGH = "01"
