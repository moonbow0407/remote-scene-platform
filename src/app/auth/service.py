"""鉴权用例：登录、刷新、用户管理。授权判断只在本模块内完成。"""

import logging
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import User
from app.auth.password import dummy_password_hash, hash_password, verify_password
from app.auth.tokens import IssuedTokens, TokenType, decode_token, issue_token_pair
from app.context import ActorContext, ActorRole, now_utc
from app.errors import conflict, forbidden, not_found, unauthorized
from app.ids import new_uuid7
from app.pagination import PageParams
from app.settings import Settings

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_user(self, user_id: UUID) -> User | None:
        return self._session.get(User, user_id)

    def get_user_required(self, user_id: UUID) -> User:
        user = self.get_user(user_id)
        if user is None:
            raise not_found("用户", user_id)
        return user

    def find_by_username_or_email(self, identifier: str) -> User | None:
        ident = identifier.strip()
        email = ident.lower()
        return self._session.scalar(
            sa.select(User).where(sa.or_(User.username == ident, User.email == email))
        )

    def create_user(
        self,
        *,
        username: str,
        email: str,
        password: str,
        role: ActorRole = ActorRole.USER,
        is_active: bool = True,
    ) -> User:
        now = now_utc()
        user = User(
            id=new_uuid7(),
            username=username.strip(),
            email=email.strip().lower(),
            password_hash=hash_password(password),
            role=role,
            is_active=is_active,
            created_at=now,
            updated_at=now,
        )
        self._session.add(user)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict("USER_ALREADY_EXISTS", "用户名或邮箱已被使用") from exc
        logger.info("用户已创建", extra={"user_id": str(user.id), "username": user.username})
        return user

    def update_user(
        self,
        user_id: UUID,
        *,
        username: str | None = None,
        email: str | None = None,
        password: str | None = None,
        role: ActorRole | None = None,
    ) -> User:
        user = self.get_user_required(user_id)
        if username is not None:
            user.username = username.strip()
        if email is not None:
            user.email = email.strip().lower()
        if password is not None:
            user.password_hash = hash_password(password)
        if role is not None:
            user.role = role
        user.updated_at = now_utc()
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict("USER_ALREADY_EXISTS", "用户名或邮箱已被使用") from exc
        return user

    def set_user_status(self, user_id: UUID, *, is_active: bool, actor: ActorContext) -> User:
        user = self.get_user_required(user_id)
        # 避免管理员禁用自己后无法再管理账号
        if actor.actor_id == str(user.id) and not is_active:
            raise forbidden("AUTH_FORBIDDEN", "不能禁用当前登录账号")
        user.is_active = is_active
        user.updated_at = now_utc()
        self._session.flush()
        return user

    def list_users(self, params: PageParams) -> tuple[list[User], int]:
        total = self._session.scalar(sa.select(sa.func.count()).select_from(User)) or 0
        items = list(
            self._session.scalars(
                sa.select(User)
                .order_by(User.created_at.desc())
                .offset(params.offset)
                .limit(params.limit)
            )
        )
        return items, total

    def login(self, *, username: str, password: str, settings: Settings) -> IssuedTokens:
        user = self.find_by_username_or_email(username)
        if user is None:
            verify_password(dummy_password_hash(), password)
            logger.info("登录失败")
            raise unauthorized("AUTH_INVALID_CREDENTIALS", "用户名或密码错误")
        if not verify_password(user.password_hash, password):
            logger.info("登录失败")
            raise unauthorized("AUTH_INVALID_CREDENTIALS", "用户名或密码错误")
        if not user.is_active:
            raise unauthorized("USER_DISABLED", "账号已禁用")
        logger.info("用户登录成功", extra={"user_id": str(user.id)})
        return self._issue_tokens(user, settings)

    def refresh(self, *, refresh_token: str, settings: Settings) -> IssuedTokens:
        claims = decode_token(
            refresh_token, secret=settings.jwt_secret, expected_type=TokenType.REFRESH
        )
        user = self.get_user(claims.user_id)
        if user is None or not user.is_active:
            raise unauthorized("AUTH_TOKEN_INVALID", "刷新令牌无效")
        # 重新签发 access 与 refresh（无服务端会话，旧 refresh 在过期前仍可使用）
        return self._issue_tokens(user, settings)

    def require_self_or_admin(self, *, actor: ActorContext, user_id: UUID) -> None:
        if actor.role == ActorRole.ADMIN:
            return
        if actor.actor_id == str(user_id):
            return
        raise forbidden("AUTH_FORBIDDEN", "不能访问其他用户")

    def _issue_tokens(self, user: User, settings: Settings) -> IssuedTokens:
        # 角色取自数据库，忽略令牌内可能被伪造的 role 声明
        return issue_token_pair(
            user_id=user.id,
            role=user.role,
            secret=settings.jwt_secret,
            access_ttl_seconds=settings.access_token_ttl_seconds,
            refresh_ttl_seconds=settings.refresh_token_ttl_seconds,
        )
