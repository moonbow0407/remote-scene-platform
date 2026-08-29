"""上传会话保存本批上传声明的业务元数据 properties。

追加版本时 AssetVersion 的元数据取自 upload_session.properties，
不再从 DataAsset.properties 抄一份（否则新版本会继承旧版本的 acquired_at）。

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONB = sa.dialects.postgresql.JSONB


def upgrade() -> None:
    # server_default 兼容迁移时已存在的行（历史 PENDING 会话按空元数据处理）
    op.add_column(
        "upload_session",
        sa.Column(
            "properties",
            JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
            comment="本批上传声明的业务元数据；完成时写入 AssetVersion.properties",
        ),
    )


def downgrade() -> None:
    op.drop_column("upload_session", "properties")
