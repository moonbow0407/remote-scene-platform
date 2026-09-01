"""把 FastAPI 默认的 OpenAPI 3.1 收成 Apifox 能稳定导入的 3.0 形状。"""

from typing import Any

_HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
_BEARER = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "JWT",
    "description": "登录后获得的 access_token。请求头 Authorization: Bearer <token>",
}


def _is_null_schema(node: object) -> bool:
    return (
        isinstance(node, dict)
        and node.get("type") == "null"
        and set(node.keys()) <= {"type", "title", "description"}
    )


def _collapse_anyof_null(node: dict[str, Any]) -> dict[str, Any]:
    any_of = node.get("anyOf")
    if not isinstance(any_of, list):
        return node
    non_null = [item for item in any_of if not _is_null_schema(item)]
    if len(non_null) != 1 or len(non_null) == len(any_of):
        return node
    inner = dict(non_null[0])
    extras = {key: value for key, value in node.items() if key != "anyOf"}
    if "$ref" in inner:
        collapsed: dict[str, Any] = {"allOf": [inner], "nullable": True}
        for key in ("title", "description", "default", "example", "examples"):
            if key in extras:
                collapsed[key] = extras[key]
            elif key in inner:
                collapsed[key] = inner[key]
        skip = {"title", "description", "default", "example", "examples"}
        for key, value in extras.items():
            if key not in collapsed and key not in skip:
                collapsed[key] = value
        return collapsed
    return {**inner, **extras, "nullable": True}


def _rewrite_numeric_bounds(node: dict[str, Any]) -> dict[str, Any]:
    mapping = (
        ("ge", "minimum"),
        ("le", "maximum"),
        ("gt", "exclusiveMinimum"),
        ("lt", "exclusiveMaximum"),
    )
    for src, dst in mapping:
        if src in node:
            node[dst] = node.pop(src)
    return node


def _rewrite_type_list(node: dict[str, Any]) -> dict[str, Any]:
    declared = node.get("type")
    if isinstance(declared, list) and "null" in declared:
        others = [item for item in declared if item != "null"]
        if len(others) == 1:
            node["type"] = others[0]
            node["nullable"] = True
    return node


def _examples_to_example(node: dict[str, Any]) -> dict[str, Any]:
    examples = node.get("examples")
    if isinstance(examples, list) and examples and "example" not in node:
        node["example"] = examples[0]
        del node["examples"]
    return node


def normalize_schema(node: Any) -> Any:
    if isinstance(node, list):
        return [normalize_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    node = {key: normalize_schema(value) for key, value in node.items()}
    node = _collapse_anyof_null(node)
    node = _rewrite_numeric_bounds(node)
    node = _rewrite_type_list(node)
    return _examples_to_example(node)


def _rewrite_security(security: Any) -> Any:
    if not isinstance(security, list):
        return security
    rewritten: list[Any] = []
    for item in security:
        if isinstance(item, dict) and "HTTPBearer" in item:
            rewritten.append({"BearerAuth": item["HTTPBearer"]})
        else:
            rewritten.append(item)
    return rewritten


def polish_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    """折叠 T | null、统一 Bearer、补 allowEmptyValue，供 Apifox 导入。"""
    schema = normalize_schema(schema)
    schema["openapi"] = "3.0.3"
    components = schema.setdefault("components", {})
    schemes = components.setdefault("securitySchemes", {})
    schemes.pop("HTTPBearer", None)
    schemes["BearerAuth"] = _BEARER
    schema["security"] = [{"BearerAuth": []}]
    for path_item in schema.get("paths", {}).values():
        if not isinstance(path_item, dict):
            continue
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            if "security" in operation:
                operation["security"] = _rewrite_security(operation["security"])
            for param in operation.get("parameters") or []:
                if not isinstance(param, dict):
                    continue
                if param.get("in") == "query" and not param.get("required"):
                    param["allowEmptyValue"] = True
    return schema
