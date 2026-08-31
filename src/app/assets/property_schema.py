"""资产 properties 的 JSON Schema 注册与校验。

首版只实现对象/基本类型/required/additionalProperties 子集，足够约束业务元数据。
完整 Draft 校验器不引入额外依赖。
"""

from __future__ import annotations

from typing import Any

from app.assets.enums import AssetType
from app.errors import validation_error

DEFAULT_PROPERTY_SCHEMAS: dict[AssetType, dict[str, Any]] = {
    AssetType.RASTER: {
        "type": "object",
        "properties": {"acquired_at": {"type": "string"}},
        "additionalProperties": True,
    },
    AssetType.VECTOR: {
        "type": "object",
        "properties": {"acquired_at": {"type": "string"}},
        "additionalProperties": True,
    },
    AssetType.ATTACHMENT: {
        "type": "object",
        "properties": {"acquired_at": {"type": "string"}},
        "additionalProperties": True,
    },
}


def default_schema_name(asset_type: AssetType) -> str:
    return f"default.{asset_type.value}"


def json_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def accumulate_property_schema(collected: dict[str, set[str]], props: dict[str, Any]) -> None:
    """把单条要素属性并入类型并集，供流式入库单遍统计。"""
    for key, value in props.items():
        collected.setdefault(str(key), set()).add(json_value_type(value))


def property_schema_from_collected(collected: dict[str, set[str]]) -> list[dict[str, Any]]:
    return [{"name": name, "types": sorted(types)} for name, types in sorted(collected.items())]


def infer_property_schema(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从要素属性推断 [{name, types}]，供 vector_asset_version.property_schema。"""
    collected: dict[str, set[str]] = {}
    for props in rows:
        accumulate_property_schema(collected, props)
    return property_schema_from_collected(collected)


def validate_properties(schema: dict[str, Any], properties: dict[str, Any]) -> None:
    """按注册的 JSON Schema 校验 properties；失败抛 422。"""
    try:
        _validate(properties, schema, path="$")
    except _SchemaError as exc:
        raise validation_error(f"properties 不满足 JSON Schema：{exc}") from exc


class _SchemaError(ValueError):
    pass


def _validate(instance: Any, schema: dict[str, Any], *, path: str) -> None:
    expected = schema.get("type")
    if expected is not None:
        allowed = expected if isinstance(expected, list) else [expected]
        actual = json_value_type(instance)
        if actual == "integer" and "number" in allowed:
            actual = "number"
        if actual not in allowed and not (actual == "null" and "null" in allowed):
            raise _SchemaError(f"{path} 期望类型 {allowed}，实际为 {actual}")
    if schema.get("type") == "object" or "properties" in schema or "required" in schema:
        if not isinstance(instance, dict):
            raise _SchemaError(f"{path} 必须是对象")
        for key in schema.get("required", []):
            if key not in instance:
                raise _SchemaError(f"{path} 缺少必填字段 {key}")
        declared = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, value in instance.items():
            child = f"{path}.{key}"
            if key in declared:
                _validate(value, declared[key], path=child)
            elif additional is False:
                raise _SchemaError(f"{path} 不允许额外字段 {key}")
            elif isinstance(additional, dict):
                _validate(value, additional, path=child)
