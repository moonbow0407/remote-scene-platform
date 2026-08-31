"""生态模块枚举。"""

from enum import StrEnum

from app.schema_docs import enum_docs


@enum_docs("启用状态", "ACTIVE：启用；DISABLED：停用。")
class EcologicalParameterStatus(StrEnum):
    """生态参数是否启用。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
