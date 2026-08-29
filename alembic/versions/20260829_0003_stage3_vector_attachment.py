"""Stage 3 矢量扩展、附件扩展、要素表与 JSON Schema 注册。

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from geoalchemy2 import Geometry

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = sa.dialects.postgresql.JSONB


def upgrade() -> None:
    op.create_table(
        "vector_asset_version",
        sa.Column(
            "asset_version_id",
            sa.Uuid(),
            sa.ForeignKey(
                "asset_version.id",
                ondelete="CASCADE",
                name="fk_vector_ext_version_id_asset_version",
            ),
            primary_key=True,
        ),
        sa.Column("crs", sa.String(128), nullable=True),
        sa.Column("user_crs", sa.String(128), nullable=True),
        sa.Column("geometry_type", sa.String(32), nullable=True),
        sa.Column("feature_count", sa.Integer(), nullable=True),
        sa.Column("native_format", sa.String(32), nullable=True),
        sa.Column("property_schema", JSONB, nullable=True),
        sa.Column("footprint", Geometry(geometry_type="POLYGON", srid=4326), nullable=True),
        sa.Column("min_x", sa.Float(), nullable=True),
        sa.Column("min_y", sa.Float(), nullable=True),
        sa.Column("max_x", sa.Float(), nullable=True),
        sa.Column("max_y", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_vector_footprint", "vector_asset_version", ["footprint"], postgresql_using="gist"
    )

    op.create_table(
        "attachment_asset_version",
        sa.Column(
            "asset_version_id",
            sa.Uuid(),
            sa.ForeignKey(
                "asset_version.id",
                ondelete="CASCADE",
                name="fk_attachment_ext_version_id_asset_version",
            ),
            primary_key=True,
        ),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column("detected_format", sa.String(32), nullable=True),
        sa.Column("original_file_name", sa.String(512), nullable=True),
    )

    op.create_table(
        "vector_feature",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "asset_version_id",
            sa.Uuid(),
            sa.ForeignKey(
                "asset_version.id",
                ondelete="CASCADE",
                name="fk_vector_feature_version_id_asset_version",
            ),
            nullable=False,
        ),
        sa.Column("geometry", Geometry(geometry_type="GEOMETRY", srid=4326), nullable=False),
        sa.Column("properties", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_vector_feature_asset_version_id", "vector_feature", ["asset_version_id"])
    op.create_index(
        "ix_vector_feature_geom", "vector_feature", ["geometry"], postgresql_using="gist"
    )

    op.create_table(
        "property_schema",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("asset_type", sa.String(16), nullable=True),
        sa.Column("schema", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("name", name="uq_property_schema_name"),
    )


def downgrade() -> None:
    op.drop_table("property_schema")
    op.drop_index("ix_vector_feature_geom", table_name="vector_feature")
    op.drop_index("ix_vector_feature_asset_version_id", table_name="vector_feature")
    op.drop_table("vector_feature")
    op.drop_table("attachment_asset_version")
    op.drop_index("ix_vector_footprint", table_name="vector_asset_version")
    op.drop_table("vector_asset_version")
