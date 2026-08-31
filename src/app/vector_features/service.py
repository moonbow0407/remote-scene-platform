"""矢量要素导入与空间检索。"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import WKTElement
from sqlalchemy.orm import Session

from app.vector_features.models import VectorFeature


class VectorFeatureService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def count_for_asset(self, asset_id: int) -> int:
        return int(
            self._session.scalar(
                sa.select(sa.func.count()).where(VectorFeature.asset_id == asset_id)
            )
            or 0
        )

    def delete_asset_features(self, asset_id: int) -> None:
        self._session.execute(sa.delete(VectorFeature).where(VectorFeature.asset_id == asset_id))

    def insert_feature_batch(self, rows: list[VectorFeature]) -> None:
        if not rows:
            return
        self._session.add_all(rows)
        self._session.flush()
        for row in rows:
            self._session.expunge(row)

    def replace_asset_features(self, asset_id: int, rows: list[VectorFeature]) -> None:
        self.delete_asset_features(asset_id)
        self.insert_feature_batch(rows)

    def search(
        self,
        *,
        asset_id: int,
        geometry_wkt: str,
        offset: int,
        limit: int,
    ) -> tuple[list[tuple[VectorFeature, dict[str, Any]]], int]:
        geom = WKTElement(geometry_wkt, srid=4326)
        filters = (
            VectorFeature.asset_id == asset_id,
            sa.func.ST_Intersects(VectorFeature.geometry, geom),
        )
        total = int(self._session.scalar(sa.select(sa.func.count()).where(*filters)) or 0)
        result = self._session.execute(
            sa.select(VectorFeature, sa.func.ST_AsGeoJSON(VectorFeature.geometry))
            .where(*filters)
            .order_by(VectorFeature.created_at, VectorFeature.id)
            .offset(offset)
            .limit(limit)
        )
        items: list[tuple[VectorFeature, dict[str, Any]]] = []
        for feature, raw in result:
            geojson = json.loads(raw) if raw else {}
            items.append((feature, geojson))
        return items, total
