"""矢量要素持久化：几何进 PostGIS，动态属性进 JSONB。"""

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import Geometry, WKTElement
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.context import now_utc
from app.db import Base


class VectorFeature(Base):
    """某一资产版本导入的要素。几何统一为 EPSG:4326。"""

    __tablename__ = "vector_feature"

    id: Mapped[Any] = mapped_column(sa.Uuid, primary_key=True)
    asset_version_id: Mapped[Any] = mapped_column(
        ForeignKey("asset_version.id", ondelete="CASCADE"), nullable=False, index=True
    )
    geometry: Mapped[WKTElement] = mapped_column(
        Geometry(geometry_type="GEOMETRY", srid=4326), nullable=False
    )
    properties: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=now_utc, nullable=False
    )

    __table_args__ = (sa.Index("ix_vector_feature_geom", "geometry", postgresql_using="gist"),)
