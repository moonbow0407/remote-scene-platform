"""Job 执行租约：lease_token / lease_expires_at / heartbeat_at。

Worker 认领时取得租约并在运行期间心跳续约；独立恢复器按"租约过期"回收失联
执行者的 RUNNING 任务并经 Outbox 重投。仅靠 Broker 重投 + started_at 阈值判定
不可靠：Worker 崩溃后消息已被 ACK，可能永远没有下一条消息。

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "job",
        sa.Column(
            "lease_token",
            sa.Uuid(),
            nullable=True,
            comment="执行租约令牌；心跳续约与恢复回收均按 token 校验归属",
        ),
    )
    op.add_column(
        "job",
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="租约到期时间；超过即视为执行者失联",
        ),
    )
    op.add_column(
        "job",
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="执行者最近一次续约时间",
        ),
    )
    op.create_index("ix_job_status_lease_expires", "job", ["status", "lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_job_status_lease_expires", table_name="job")
    op.drop_column("job", "heartbeat_at")
    op.drop_column("job", "lease_expires_at")
    op.drop_column("job", "lease_token")
