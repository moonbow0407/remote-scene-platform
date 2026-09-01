"""启动时幂等创建引导管理员。

只应由 API 进程在 lifespan 中调用。口令不得写入日志。
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.auth.models import User
from app.auth.schemas import _validate_email, _validate_password, _validate_username
from app.auth.service import AuthService
from app.context import ActorRole
from app.db import session_scope
from app.errors import ProblemError
from app.settings import Settings

logger = logging.getLogger(__name__)


def bootstrap_admin(factory: sessionmaker[Session], settings: Settings) -> None:
    """按配置创建首个 ADMIN；生产环境启动结束时必须已有启用的管理员。"""
    username = settings.bootstrap_admin_username.strip()
    email = settings.bootstrap_admin_email.strip()
    password = settings.bootstrap_admin_password
    if username:
        _create_if_absent(factory, username=username, email=email, password=password)
    if settings.is_production:
        _require_active_admin(factory)


def _create_if_absent(
    factory: sessionmaker[Session], *, username: str, email: str, password: str
) -> None:
    try:
        username = _validate_username(username)
        email = _validate_email(email)
        password = _validate_password(password)
    except ValueError as exc:
        raise RuntimeError(f"引导管理员配置不合法：{exc}") from exc

    with session_scope(factory) as session:
        service = AuthService(session)
        existing = service.find_by_username_or_email(username) or service.find_by_username_or_email(
            email
        )
        if existing is not None:
            logger.info(
                "引导管理员已存在，跳过",
                extra={"user_id": str(existing.id), "username": existing.username},
            )
            return
        try:
            user = service.create_user(
                username=username,
                email=email,
                password=password,
                role=ActorRole.ADMIN,
            )
        except ProblemError as exc:
            if exc.code != "USER_ALREADY_EXISTS":
                raise
            logger.info("引导管理员已存在，跳过")
            return
        logger.info(
            "已创建引导管理员",
            extra={"user_id": str(user.id), "username": user.username},
        )


def _require_active_admin(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        count = session.scalar(
            sa.select(sa.func.count())
            .select_from(User)
            .where(User.role == ActorRole.ADMIN, User.is_active.is_(True))
        )
    if not count:
        raise RuntimeError(
            "生产环境必须存在至少一名启用的管理员；"
            "请配置 APP_BOOTSTRAP_ADMIN_USERNAME / EMAIL / PASSWORD，或预先写入 app_user"
        )
