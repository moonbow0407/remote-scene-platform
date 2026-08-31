"""瓦片受控访问：短期 HMAC 令牌的签发与校验。

TiTiler 与 MinIO 不直接暴露；客户端仅持有绑定具体资产版本、短期有效的
令牌，经 Nginx auth_request 交给本模块校验。签名同时绑定实际 COG S3 URL，令牌格式：
`v1.{version_id}.{过期Unix秒}.{HMAC-SHA256 前 32 位 hex}`
"""

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from app.context import now_utc
from app.errors import ProblemError
from app.ids import new_uuid7

_TOKEN_PREFIX = "v1"


class TileTokenError(ProblemError):
    def __init__(self, detail: str, status: int = 401) -> None:
        super().__init__(
            status=status, code="TILE_TOKEN_INVALID", title="瓦片令牌无效", detail=detail
        )


def sign_tile_token(
    *, asset_id: str, resource: str, ttl_seconds: int, secret: str
) -> tuple[str, int]:
    """签发绑定资产、短期有效的瓦片令牌；返回 (token, 过期Unix秒)。"""
    if not secret:
        raise ProblemError(
            status=503,
            code="TILE_TOKEN_SECRET_MISSING",
            title="瓦片令牌密钥未配置",
            detail="缺少 APP_TILE_TOKEN_SECRET 配置，拒绝签发瓦片令牌",
        )
    expires_at = int(now_utc().timestamp()) + ttl_seconds
    payload = f"{_TOKEN_PREFIX}.{asset_id}.{expires_at}"
    signature = hmac.new(
        secret.encode(), f"{payload}.{resource}".encode(), hashlib.sha256
    ).hexdigest()[:32]
    return f"{payload}.{signature}", expires_at


def verify_tile_token(token: str, *, resource: str, secret: str) -> str:
    """校验令牌签名与有效期，返回绑定的 asset_version_id；失败抛 401。"""
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != _TOKEN_PREFIX:
        raise TileTokenError("令牌格式不合法")
    _, asset_id, expires_raw, signature = parts
    if not secret:
        raise TileTokenError("服务端未配置瓦片令牌密钥", status=503)
    expected = hmac.new(
        secret.encode(),
        f"{_TOKEN_PREFIX}.{asset_id}.{expires_raw}.{resource}".encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        raise TileTokenError("令牌签名不匹配")
    if int(expires_raw) < int(time.time()):
        raise TileTokenError("令牌已过期")
    return asset_id


def extract_token_from_uri(uri: str) -> str:
    """从 Nginx auth_request 传来的 X-Original-URI 中提取 token 查询参数。"""
    query = urlsplit(uri).query
    values = parse_qs(query).get("token", [])
    if not values:
        raise TileTokenError("缺少 token 查询参数")
    return values[0]


def extract_resource_from_uri(uri: str) -> str:
    """提取 TiTiler 的 url 参数；它必须参与 HMAC 校验，防止令牌跨对象复用。"""
    values = parse_qs(urlsplit(uri).query).get("url", [])
    if len(values) != 1 or not values[0].startswith("s3://"):
        raise TileTokenError("缺少或非法的 COG url 参数")
    return values[0]


def build_tile_url_template(
    *,
    base_url: str,
    cog_object_key: str,
    bucket: str,
    token: str,
    band_indexes: list[int],
) -> dict[str, Any]:
    """生成经 Nginx 网关的瓦片 URL 模板（TiTiler /cog 路由）。"""
    if not band_indexes or any(index < 1 for index in band_indexes):
        raise ValueError("瓦片波段索引必须为非空正整数列表")

    s3_url = f"s3://{bucket}/{cog_object_key}"
    query = urlencode(
        [("url", s3_url), *(("bidx", str(index)) for index in band_indexes), ("token", token)]
    )
    return {
        "tile_url_template": (
            f"{base_url}/tiles/cog/tiles/WebMercatorQuad/{{z}}/{{x}}/{{y}}.png?{query}"
        ),
        "tile_json_url": f"{base_url}/tiles/cog/WebMercatorQuad/tilejson.json?{query}",
    }


def new_request_id() -> str:
    return str(new_uuid7())
