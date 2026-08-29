"""鉴权与用户管理路由：HTTP 适配层，JWT 细节不进入业务 Service。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.auth.dependencies import (
    get_current_actor,
    get_current_user,
    get_session,
    require_admin,
)
from app.auth.models import User
from app.auth.schemas import (
    LoginRequest,
    RefreshRequest,
    TokenResponse,
    UserCreateRequest,
    UserPublic,
    UserStatusRequest,
    UserUpdateRequest,
)
from app.auth.service import AuthService
from app.auth.tokens import IssuedTokens
from app.context import ActorContext
from app.pagination import Page, PageParams

auth_router = APIRouter(prefix="/auth", tags=["鉴权"])
users_router = APIRouter(prefix="/users", tags=["用户"])


def _service(session: Annotated[Session, Depends(get_session)]) -> AuthService:
    return AuthService(session)


def _token_response(issued: IssuedTokens) -> TokenResponse:
    return TokenResponse(
        access_token=issued.access_token,
        refresh_token=issued.refresh_token,
        token_type=issued.token_type,
        expires_in=issued.expires_in,
    )


@auth_router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    service: Annotated[AuthService, Depends(_service)],
) -> TokenResponse:
    issued = service.login(
        username=body.username, password=body.password, settings=request.app.state.settings
    )
    return _token_response(issued)


@auth_router.post("/refresh", response_model=TokenResponse)
def refresh(
    body: RefreshRequest,
    request: Request,
    service: Annotated[AuthService, Depends(_service)],
) -> TokenResponse:
    issued = service.refresh(refresh_token=body.refresh_token, settings=request.app.state.settings)
    return _token_response(issued)


@auth_router.get("/me", response_model=UserPublic)
def me(user: Annotated[User, Depends(get_current_user)]) -> UserPublic:
    return UserPublic.model_validate(user)


@users_router.get("", response_model=Page[UserPublic])
def list_users(
    params: Annotated[PageParams, Depends()],
    _admin: Annotated[ActorContext, Depends(require_admin)],
    service: Annotated[AuthService, Depends(_service)],
) -> Page[UserPublic]:
    items, total = service.list_users(params)
    return Page.build(
        items=[UserPublic.model_validate(item) for item in items],
        total=total,
        params=params,
    )


@users_router.post("", status_code=201, response_model=UserPublic)
def create_user(
    body: UserCreateRequest,
    _admin: Annotated[ActorContext, Depends(require_admin)],
    service: Annotated[AuthService, Depends(_service)],
) -> UserPublic:
    user = service.create_user(
        username=body.username,
        email=body.email,
        password=body.password,
        role=body.role,
    )
    return UserPublic.model_validate(user)


@users_router.get("/{user_id}", response_model=UserPublic)
def get_user(
    user_id: UUID,
    actor: Annotated[ActorContext, Depends(get_current_actor)],
    service: Annotated[AuthService, Depends(_service)],
) -> UserPublic:
    service.require_self_or_admin(actor=actor, user_id=user_id)
    return UserPublic.model_validate(service.get_user_required(user_id))


@users_router.put("/{user_id}", response_model=UserPublic)
def update_user(
    user_id: UUID,
    body: UserUpdateRequest,
    _admin: Annotated[ActorContext, Depends(require_admin)],
    service: Annotated[AuthService, Depends(_service)],
) -> UserPublic:
    user = service.update_user(
        user_id,
        username=body.username,
        email=body.email,
        password=body.password,
        role=body.role,
    )
    return UserPublic.model_validate(user)


@users_router.patch("/{user_id}/status", response_model=UserPublic)
def set_user_status(
    user_id: UUID,
    body: UserStatusRequest,
    admin: Annotated[ActorContext, Depends(require_admin)],
    service: Annotated[AuthService, Depends(_service)],
) -> UserPublic:
    user = service.set_user_status(user_id, is_active=body.is_active, actor=admin)
    return UserPublic.model_validate(user)
