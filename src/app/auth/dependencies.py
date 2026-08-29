"""FastAPI 鉴权依赖：JWT → User → ActorContext。

当前只在 auth/users 路由使用，不批量加到既有业务接口。
授权以数据库中的用户角色为准，不信任令牌内的 role 声明。
"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.models import User, user_to_actor
from app.auth.tokens import TokenType, decode_token
from app.context import ActorContext, ActorRole, get_actor
from app.db import session_scope
from app.errors import forbidden, unauthorized
from app.settings import Settings

_bearer = HTTPBearer(auto_error=False)


def get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _extract_bearer(
    credentials: HTTPAuthorizationCredentials | None, *, required: bool
) -> str | None:
    if credentials is None:
        if required:
            raise unauthorized("AUTH_REQUIRED", "缺少有效的 Bearer 访问令牌")
        return None
    if credentials.scheme.lower() != "bearer":
        raise unauthorized("AUTH_TOKEN_INVALID", "认证方案必须为 Bearer")
    token = credentials.credentials.strip()
    if not token:
        if required:
            raise unauthorized("AUTH_REQUIRED", "缺少有效的 Bearer 访问令牌")
        return None
    return token


def get_current_user(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    token = _extract_bearer(credentials, required=True)
    if token is None:
        raise unauthorized("AUTH_REQUIRED", "缺少有效的 Bearer 访问令牌")
    settings = _settings(request)
    claims = decode_token(token, secret=settings.jwt_secret, expected_type=TokenType.ACCESS)
    user = session.get(User, claims.user_id)
    if user is None:
        raise unauthorized("AUTH_TOKEN_INVALID", "访问令牌无效")
    if not user.is_active:
        raise unauthorized("USER_DISABLED", "账号已禁用")
    return user


def get_current_actor(user: Annotated[User, Depends(get_current_user)]) -> ActorContext:
    """已认证用户 → ActorContext，供后续业务 Service 复用。"""
    return user_to_actor(user)


def get_optional_actor(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ActorContext:
    """未带令牌时返回匿名 Actor；带了令牌则必须通过校验（失败闭合）。"""
    token = _extract_bearer(credentials, required=False)
    if token is None:
        return get_actor()
    settings = _settings(request)
    claims = decode_token(token, secret=settings.jwt_secret, expected_type=TokenType.ACCESS)
    user = session.get(User, claims.user_id)
    if user is None:
        raise unauthorized("AUTH_TOKEN_INVALID", "访问令牌无效")
    if not user.is_active:
        raise unauthorized("USER_DISABLED", "账号已禁用")
    return user_to_actor(user)


def require_authenticated_actor(
    actor: Annotated[ActorContext, Depends(get_current_actor)],
) -> ActorContext:
    return actor


def require_admin(
    actor: Annotated[ActorContext, Depends(get_current_actor)],
) -> ActorContext:
    if actor.role != ActorRole.ADMIN:
        raise forbidden("AUTH_FORBIDDEN", "需要管理员权限")
    return actor
