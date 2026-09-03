"""影像表硬删除：去掉回收站、缩略图、bbox 冗余列。

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROP_COLUMNS = (
    "thumbnail_object_key",
    "min_x",
    "min_y",
    "max_x",
    "max_y",
    "created_by",
    "deleted_at",
    "purge_after",
    "deleted_by",
    "purge_attempts",
    "purge_next_attempt_at",
    "purge_last_error",
)


def upgrade() -> None:
    for table in ("satellite_data", "uav_data"):
        op.drop_index(f"ix_{table}_purge_due", table_name=table)
        for column in _DROP_COLUMNS:
            op.drop_column(table, column)


def downgrade() -> None:
    raise NotImplementedError("0015 为破坏性迁移，不支持自动降级")
