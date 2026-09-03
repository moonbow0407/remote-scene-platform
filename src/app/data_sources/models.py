"""产品型号字典。"""

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from app.data_sources.enums import DataSourceStatus
from app.db import Base, TimestampMixin
from app.imagery.enums import RecordKind


class DataSource(Base, TimestampMixin):
    """反演/检索用的产品型号，不是某一景文件。"""

    __tablename__ = "data_source"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(
        sa.String(16), nullable=False, unique=True, comment="六位编号，0001xx 卫星、0002xx 无人机"
    )
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False, comment="显示名称")
    kind: Mapped[RecordKind] = mapped_column(
        sa.Enum(RecordKind, native_enum=False, length=16),
        nullable=False,
        index=True,
        comment="SATELLITE/UAV，须与 code 前四位一致",
    )
    status: Mapped[DataSourceStatus] = mapped_column(
        sa.Enum(DataSourceStatus, native_enum=False, length=16),
        nullable=False,
        default=DataSourceStatus.ACTIVE,
        index=True,
    )
