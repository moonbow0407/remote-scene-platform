"""FastAPI 鉴权依赖：JWT → User → ActorContext。

应用级默认拒绝匿名；白名单见 access.py。
授权以数据库中的用户角色为准，不信任令牌内的 role 声明。
"""

from collections.abc import AsyncIterator, Iterator
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.access import is_public_request
from app.auth.models import User, user_to_actor
from app.auth.tokens import TokenType, decode_token
from app.context import ActorContext, ActorRole, bind_actor, get_actor
from app.db import session_scope
from app.errors import forbidden, unauthorized
from app.settings import Settings

_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    bearerFormat="JWT",
    description="登录后获得的 access_token。请求头 Authorization: Bearer <token>",
)


def get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _extract_bearer(credentials: HTTPAuthorizationCredentials | None) -> str:
    if credentials is None:
        raise unauthorized("AUTH_REQUIRED", "缺少有效的 Bearer 访问令牌")
    if credentials.scheme.lower() != "bearer":
        raise unauthorized("AUTH_TOKEN_INVALID", "认证方案必须为 Bearer")
    token = credentials.credentials.strip()
    if not token:
        raise unauthorized("AUTH_REQUIRED", "缺少有效的 Bearer 访问令牌")
    return token


def _load_user_from_access_token(
    request: Request,
    session: Session,
    credentials: HTTPAuthorizationCredentials | None,
) -> User:
    token = _extract_bearer(credentials)
    settings = _settings(request)
    claims = decode_token(token, secret=settings.jwt_secret, expected_type=TokenType.ACCESS)
    user = session.get(User, claims.user_id)
    if user is None:
        raise unauthorized("AUTH_TOKEN_INVALID", "访问令牌无效")
    if not user.is_active:
        raise unauthorized("USER_DISABLED", "账号已禁用")
    return user


def get_current_user(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    return _load_user_from_access_token(request, session, credentials)


def get_current_actor() -> ActorContext:
    """已认证用户。依赖应用级鉴权把 ActorContext 绑到当前请求。"""
    actor = get_actor()
    if actor.actor_id is None:
        raise unauthorized("AUTH_REQUIRED", "缺少有效的 Bearer 访问令牌")
    return actor


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


async def enforce_request_actor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AsyncIterator[ActorContext | None]:
    """应用级默认拒绝：白名单匿名，其余必须登录并把 Actor 绑到请求。

    必须是 async：ContextVar 在事件循环任务上绑定，同步路由经 anyio
    拷贝上下文进线程池后才能读到当前用户。
    """
    if is_public_request(request.method, request.url.path):
        yield None
        return
    with session_scope(request.app.state.session_factory) as session:
        user = _load_user_from_access_token(request, session, credentials)
        actor = user_to_actor(user)
    with bind_actor(actor):
        yield actor
