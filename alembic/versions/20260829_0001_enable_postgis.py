"""启用 PostGIS 扩展

PostgreSQL/PostGIS 是唯一关系与空间数据库；其余结构随各阶段业务迁移添加。

Revision ID: 0001
Revises:
Create Date: 2026-08-29

"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")


def downgrade() -> None:
    # postgis/postgis 基础镜像经 template1 预置了配套扩展，且它们依赖 postgis，
    # 必须按依赖顺序先卸载从属扩展，否则 DROP postgis 报 DependentObjectsStillExist
    op.execute("DROP EXTENSION IF EXISTS postgis_tiger_geocoder")
    op.execute("DROP EXTENSION IF EXISTS postgis_topology")
    op.execute("DROP EXTENSION IF EXISTS fuzzystrmatch")
    op.execute("DROP EXTENSION IF EXISTS postgis")
