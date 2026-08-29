"""目录模块枚举。"""

from enum import StrEnum


class CatalogStatus(StrEnum):
    """目录条目启用状态。"""

    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
