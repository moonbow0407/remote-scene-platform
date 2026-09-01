"""GET 查询参数约定：空字符串视为未传。JSON 请求体不做此兼容。"""

from typing import Any

from pydantic import BeforeValidator


def blank_as_none(value: Any) -> Any:
    """把空串和纯空白当成缺省，其余原样交给后续类型校验。"""
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def blank_as_default(default: Any) -> BeforeValidator:
    """有默认值的查询参数：空串回落到默认值，以便继续做 ge/le 等约束。"""

    def _inner(value: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, str) and value.strip() == "":
            return default
        return value

    return BeforeValidator(_inner)


BlankAsNone = BeforeValidator(blank_as_none)
