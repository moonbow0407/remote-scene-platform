"""上传会话持久化模型。"""

from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin
from app.schema_docs import enum_docs


@enum_docs(
    "上传状态",
    "PENDING：还在传分片；COMPLETED：已合并并开始处理；ABORTED：已中止。",
)
class UploadSessionStatus(StrEnum):
    """上传会话状态。"""

    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class UploadSession(Base, TimestampMixin):
    """MinIO Multipart 上传会话。一个会话对应一条新资产。"""

    __tablename__ = "upload_session"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("data_asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[UploadSessionStatus] = mapped_column(
        sa.Enum(UploadSessionStatus, native_enum=False, length=16),
        nullable=False,
        default=UploadSessionStatus.PENDING,
    )
    minio_upload_id: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    object_key: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    file_name: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    part_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    content_type: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_error: Mapped[dict | None] = mapped_column(sa.JSON, nullable=True)
