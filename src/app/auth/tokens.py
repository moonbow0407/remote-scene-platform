"""Access / Refresh JWT 签发与校验。

传输细节停留在鉴权层：业务 Service 不解析令牌。
不在日志或异常详情中输出完整令牌。
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID

import jwt

from app.context import ActorRole, now_utc
from app.errors import service_unavailable, unauthorized

_JWT_ALGORITHM = "HS256"


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


@dataclass(frozen=True)
class TokenClaims:
    """已验证的令牌声明。role 仅 Access Token 携带，授权仍以数据库角色为准。"""

    user_id: UUID
    token_type: TokenType
    role: ActorRole | None
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int


def _require_secret(secret: str) -> str:
    cleaned = secret.strip()
    if not cleaned:
        raise service_unavailable(
            "JWT",
            "缺少 APP_JWT_SECRET 配置，拒绝签发或校验令牌",
        )
    return cleaned


def issue_access_token(
    *,
    user_id: UUID,
    role: ActorRole,
    secret: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    """签发 Access Token；payload 含 sub/role/token_type/iat/exp。"""
    return _encode(
        user_id=user_id,
        token_type=TokenType.ACCESS,
        secret=secret,
        ttl_seconds=ttl_seconds,
        now=now,
        role=role,
    )


def issue_refresh_token(
    *,
    user_id: UUID,
    secret: str,
    ttl_seconds: int,
    now: datetime | None = None,
) -> str:
    """签发 Refresh Token；不含 role，且 token_type 与 Access 区分。"""
    return _encode(
        user_id=user_id,
        token_type=TokenType.REFRESH,
        secret=secret,
        ttl_seconds=ttl_seconds,
        now=now,
        role=None,
    )


def issue_token_pair(
    *,
    user_id: UUID,
    role: ActorRole,
    secret: str,
    access_ttl_seconds: int,
    refresh_ttl_seconds: int,
    now: datetime | None = None,
) -> IssuedTokens:
    issued_at = now or now_utc()
    return IssuedTokens(
        access_token=issue_access_token(
            user_id=user_id,
            role=role,
            secret=secret,
            ttl_seconds=access_ttl_seconds,
            now=issued_at,
        ),
        refresh_token=issue_refresh_token(
            user_id=user_id,
            secret=secret,
            ttl_seconds=refresh_ttl_seconds,
            now=issued_at,
        ),
        token_type="Bearer",
        expires_in=access_ttl_seconds,
    )


def decode_token(token: str, *, secret: str, expected_type: TokenType) -> TokenClaims:
    """验证签名、过期与 token type；失败统一 401，不泄露令牌内容。"""
    key = _require_secret(secret)
    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=[_JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise unauthorized("AUTH_TOKEN_EXPIRED", "令牌已过期") from exc
    except jwt.InvalidTokenError as exc:
        raise unauthorized("AUTH_TOKEN_INVALID", "令牌无效") from exc

    raw_type = payload.get("token_type")
    try:
        token_type = TokenType(raw_type)
    except ValueError as exc:
        raise unauthorized("AUTH_TOKEN_INVALID", "令牌类型无效") from exc
    if token_type != expected_type:
        raise unauthorized("AUTH_TOKEN_INVALID", "令牌类型不匹配")

    try:
        user_id = UUID(str(payload["sub"]))
    except (ValueError, TypeError) as exc:
        raise unauthorized("AUTH_TOKEN_INVALID", "令牌主体无效") from exc

    role: ActorRole | None = None
    raw_role = payload.get("role")
    if raw_role is not None:
        try:
            role = ActorRole(raw_role)
        except ValueError as exc:
            raise unauthorized("AUTH_TOKEN_INVALID", "令牌角色无效") from exc

    return TokenClaims(
        user_id=user_id,
        token_type=token_type,
        role=role,
        issued_at=datetime.fromtimestamp(int(payload["iat"]), tz=UTC),
        expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC),
    )


def _encode(
    *,
    user_id: UUID,
    token_type: TokenType,
    secret: str,
    ttl_seconds: int,
    now: datetime | None,
    role: ActorRole | None,
) -> str:
    key = _require_secret(secret)
    issued_at = now or now_utc()
    payload: dict[str, object] = {
        "sub": str(user_id),
        "token_type": token_type.value,
        "iat": issued_at,
        "exp": issued_at + timedelta(seconds=ttl_seconds),
    }
    if role is not None:
        payload["role"] = role.value
    return jwt.encode(payload, key, algorithm=_JWT_ALGORITHM)
