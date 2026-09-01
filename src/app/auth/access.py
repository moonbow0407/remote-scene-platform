"""匿名白名单：登录、探活、文档、指标与瓦片校验。

必须与 `app.api.app.API_V1_PREFIX` 保持一致。新的匿名路径只能加在这里，
并由 OpenAPI 巡检测试锁住。
"""

from __future__ import annotations

API_V1_PREFIX = "/api/v1"

# (METHOD, 规范化路径) 精确匹配。规范化会去掉末尾斜杠。
PUBLIC_EXACT: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", f"{API_V1_PREFIX}/auth/login"),
        ("POST", f"{API_V1_PREFIX}/auth/refresh"),
        ("GET", f"{API_V1_PREFIX}/healthz"),
        ("GET", f"{API_V1_PREFIX}/readyz"),
        ("GET", f"{API_V1_PREFIX}/metrics"),
        ("GET", f"{API_V1_PREFIX}/tiles/verify"),
        ("GET", f"{API_V1_PREFIX}/openapi.json"),
    }
)

# (METHOD, 前缀) 前缀匹配，覆盖 Swagger UI 子路径。
PUBLIC_PREFIXES: tuple[tuple[str, str], ...] = (("GET", f"{API_V1_PREFIX}/docs"),)


def normalize_path(path: str) -> str:
    """去掉末尾斜杠；根路径保持为 /。"""
    if path == "/":
        return path
    return path.rstrip("/") or "/"


def is_public_request(method: str, path: str) -> bool:
    """该请求是否允许匿名。方法按大写比较。"""
    verb = method.upper()
    normalized = normalize_path(path)
    if (verb, normalized) in PUBLIC_EXACT:
        return True
    for prefix_method, prefix in PUBLIC_PREFIXES:
        if verb == prefix_method and (normalized == prefix or normalized.startswith(f"{prefix}/")):
            return True
    return False
