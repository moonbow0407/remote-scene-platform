"""去掉生态参量字典上闲置的 created_by。

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("ecological_parameter", "created_by")


def downgrade() -> None:
    op.add_column(
        "ecological_parameter",
        sa.Column("created_by", sa.Integer(), nullable=True, comment="鉴权预留：创建者"),
    )
