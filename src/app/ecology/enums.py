"""生态模块枚举。"""

from enum import StrEnum


class EcologicalParameterStatus(StrEnum):
    """生态参数启用状态。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
