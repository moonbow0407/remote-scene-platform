"""鉴权：app_user 表（ADMIN/USER，Argon2 口令哈希）。

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "app_user",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, comment="登录名，全局唯一"),
        sa.Column("email", sa.String(255), nullable=False, comment="邮箱，规范化为小写后全局唯一"),
        sa.Column(
            "password_hash", sa.String(255), nullable=False, comment="Argon2id 哈希，禁止明文"
        ),
        sa.Column("role", sa.String(16), nullable=False, comment="ADMIN/USER"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("username", name="uq_app_user_username"),
        sa.UniqueConstraint("email", name="uq_app_user_email"),
    )
    op.create_index("ix_app_user_is_active", "app_user", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_app_user_is_active", table_name="app_user")
    op.drop_table("app_user")
