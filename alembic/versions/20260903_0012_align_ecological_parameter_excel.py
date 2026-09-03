"""对齐生态参量字典：细项编号、大类字段，去掉 parent_id。

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.ecology.seed_data import seed_items

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAJOR_SQL = (
    "CASE LEFT(code, 2) "
    "WHEN '01' THEN '生物参数' "
    "WHEN '02' THEN '土壤参数' "
    "WHEN '03' THEN '大气参数' "
    "WHEN '04' THEN '水文地质参数' "
    "WHEN '05' THEN '开采相关参数' "
    "WHEN '06' THEN '双碳参数' "
    "WHEN '07' THEN '水体参数' "
    "ELSE '未分类' END"
)


def upgrade() -> None:
    op.add_column("ecological_parameter", sa.Column("abbrev", sa.String(64), nullable=True))
    op.add_column("ecological_parameter", sa.Column("english_name", sa.String(255), nullable=True))
    op.add_column("ecological_parameter", sa.Column("major_code", sa.String(8), nullable=True))
    op.add_column("ecological_parameter", sa.Column("major_name", sa.String(255), nullable=True))
    op.add_column("ecological_parameter", sa.Column("remark", sa.Text(), nullable=True))

    op.execute(
        sa.text(
            "UPDATE ecological_parameter SET "
            "major_code = LEFT(code, 2), "
            f"major_name = {_MAJOR_SQL}, "
            "abbrev = code "
            "WHERE code ~ '^[0-9]{4}$'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE ecological_parameter SET abbrev = 'TMP-' || id::text "
            "WHERE abbrev IS NULL OR btrim(abbrev) = ''"
        )
    )
    op.execute(
        sa.text(
            "UPDATE ecological_parameter SET "
            "major_code = COALESCE(major_code, '00'), "
            "major_name = COALESCE(major_name, '未分类') "
            "WHERE major_code IS NULL"
        )
    )

    op.alter_column("ecological_parameter", "abbrev", nullable=False)
    op.alter_column("ecological_parameter", "major_code", nullable=False)
    op.alter_column("ecological_parameter", "major_name", nullable=False)
    op.create_unique_constraint(
        "uq_ecological_parameter_abbrev", "ecological_parameter", ["abbrev"]
    )
    op.create_index("ix_ecological_parameter_major_code", "ecological_parameter", ["major_code"])
    op.drop_index("ix_ecological_parameter_parent_sort", table_name="ecological_parameter")
    op.drop_index("ix_ecological_parameter_parent_id", table_name="ecological_parameter")
    op.drop_constraint(
        "fk_ecological_parameter_parent_id_ecological_parameter",
        "ecological_parameter",
        type_="foreignkey",
    )
    op.drop_column("ecological_parameter", "parent_id")

    insert_sql = sa.text(
        """
        INSERT INTO ecological_parameter
            (code, name, abbrev, english_name, major_code, major_name,
             remark, status, sort_order)
        VALUES
            (:code, :name, :abbrev, :english_name, :major_code, :major_name,
             NULL, 'ACTIVE', 0)
        ON CONFLICT (code) DO UPDATE SET
            name = EXCLUDED.name,
            abbrev = EXCLUDED.abbrev,
            english_name = EXCLUDED.english_name,
            major_code = EXCLUDED.major_code,
            major_name = EXCLUDED.major_name
        """
    )
    bind = op.get_bind()
    for item in seed_items():
        bind.execute(insert_sql, item)


def downgrade() -> None:
    op.add_column(
        "ecological_parameter",
        sa.Column("parent_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ecological_parameter_parent_id_ecological_parameter",
        "ecological_parameter",
        "ecological_parameter",
        ["parent_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_ecological_parameter_parent_id", "ecological_parameter", ["parent_id"])
    op.create_index(
        "ix_ecological_parameter_parent_sort",
        "ecological_parameter",
        ["parent_id", "sort_order"],
    )
    op.drop_index("ix_ecological_parameter_major_code", table_name="ecological_parameter")
    op.drop_constraint("uq_ecological_parameter_abbrev", "ecological_parameter", type_="unique")
    op.drop_column("ecological_parameter", "remark")
    op.drop_column("ecological_parameter", "major_name")
    op.drop_column("ecological_parameter", "major_code")
    op.drop_column("ecological_parameter", "english_name")
    op.drop_column("ecological_parameter", "abbrev")
