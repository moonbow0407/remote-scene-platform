"""平铺分类：名称唯一，没有父节点。"""

from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin


class Category(Base, TimestampMixin):
    """业务分类，供资产归类与列表过滤。"""

    __tablename__ = "category"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        sa.String(255), nullable=False, unique=True, comment="显示名称，全局唯一"
    )
    created_by: Mapped[Any | None] = mapped_column(sa.Integer, nullable=True)
