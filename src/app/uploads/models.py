"""上传会话持久化模型。"""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin


class UploadSessionStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class UploadSession(Base, TimestampMixin):
    """MinIO Multipart 上传会话。

    一个会话对应逻辑资产的一个未来版本；分片经预签名 URL 直传 MinIO，
    文件字节不经过 API 进程。
    """

    __tablename__ = "upload_session"

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    asset_id: Mapped[UUID] = mapped_column(
        ForeignKey("data_asset.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[UploadSessionStatus] = mapped_column(
        sa.Enum(UploadSessionStatus, native_enum=False, length=16),
        nullable=False,
        default=UploadSessionStatus.PENDING,
        comment="PENDING/COMPLETED/ABORTED",
    )
    minio_upload_id: Mapped[str] = mapped_column(sa.String(256), nullable=False)
    object_key: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    file_name: Mapped[str] = mapped_column(sa.String(512), nullable=False)
    size_bytes: Mapped[int] = mapped_column(sa.BigInteger, nullable=False)
    part_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    content_type: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    completed_version_id: Mapped[UUID | None] = mapped_column(sa.Uuid, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
