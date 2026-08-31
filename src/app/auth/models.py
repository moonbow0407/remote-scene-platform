"""用户持久化模型。

不复制若依用户/部门/岗位/菜单体系；角色仅 ADMIN/USER。
表名避开 PostgreSQL 保留字 `user`。
"""

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.context import ActorContext, ActorRole
from app.db import Base, TimestampMixin


class User(Base, TimestampMixin):
    """平台用户：口令以 Argon2id 哈希持久化，禁止明文。"""

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, unique=True, comment="登录名，全局唯一"
    )
    email: Mapped[str] = mapped_column(
        sa.String(255), nullable=False, unique=True, comment="邮箱，规范化为小写后全局唯一"
    )
    password_hash: Mapped[str] = mapped_column(
        sa.String(255), nullable=False, comment="Argon2id 哈希，禁止明文"
    )
    role: Mapped[ActorRole] = mapped_column(
        sa.Enum(ActorRole, native_enum=False, length=16),
        nullable=False,
        default=ActorRole.USER,
        comment="ADMIN/USER",
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, index=True, comment="False 表示禁用，拒绝登录"
    )


def user_to_actor(user: User) -> ActorContext:
    """把已认证用户映射为既有 ActorContext；不含 JWT 与口令。"""
    return ActorContext(
        actor_id=str(user.id),
        display_name=user.username,
        role=user.role,
    )
