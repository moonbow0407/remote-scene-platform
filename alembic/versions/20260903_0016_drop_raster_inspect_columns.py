"""去掉影像表上闲置的栅格探查列；渲染波段申请瓦片时现算。

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DROP_COLUMNS = (
    "width",
    "height",
    "bands",
    "resolution_x",
    "resolution_y",
    "nodata",
    "render_profile",
)


def upgrade() -> None:
    for table in ("satellite_data", "uav_data"):
        for column in _DROP_COLUMNS:
            op.drop_column(table, column)


def downgrade() -> None:
    raise NotImplementedError("0016 为破坏性迁移，不支持自动降级")
