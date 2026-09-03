"""矿山基础信息读写服务。"""

from __future__ import annotations

import sqlalchemy as sa
from geoalchemy2 import WKTElement
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.errors import conflict, not_found, validation_error
from app.mines.models import Mine
from app.mines.schemas import MineCreate, MineUpdate
from app.pagination import Page, PageParams


class MineService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get_required(self, mine_id: str) -> Mine:
        row = self._session.get(Mine, mine_id)
        if row is None:
            raise not_found("矿山", mine_id)
        return row

    def list(
        self, params: PageParams, *, q: str | None, mine_province: str | None
    ) -> Page[Mine]:
        stmt = sa.select(Mine)
        count_stmt = sa.select(sa.func.count()).select_from(Mine)
        if q:
            pattern = f"%{q.strip()}%"
            condition = sa.or_(Mine.mine_id.ilike(pattern), Mine.mine_name.ilike(pattern))
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        if mine_province:
            stmt = stmt.where(Mine.mine_province == mine_province)
            count_stmt = count_stmt.where(Mine.mine_province == mine_province)
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(Mine.mine_name, Mine.mine_id)
                .offset(params.offset)
                .limit(params.limit)
            )
        )
        return Page.build(rows, total, params)

    def create(self, body: MineCreate, boundary_wkt: str) -> Mine:
        row = Mine(
            **body.model_dump(exclude={"spatial_geojson"}),
            boundary_polygon=WKTElement(boundary_wkt, srid=4326),
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict("MINE_ID_CONFLICT", f"矿山编号 {body.mine_id} 已存在") from exc
        return row

    def update(self, mine_id: str, body: MineUpdate, boundary_wkt: str | None) -> Mine:
        row = self.get_required(mine_id)
        data = body.model_dump(exclude_unset=True, exclude={"spatial_geojson"})
        for field, value in data.items():
            setattr(row, field, value)
        if (
            row.mine_elevation_lower is not None
            and row.mine_elevation_upper is not None
            and row.mine_elevation_lower > row.mine_elevation_upper
        ):
            raise validation_error("最高海拔不能小于最低海拔")
        if boundary_wkt is not None:
            row.boundary_polygon = WKTElement(boundary_wkt, srid=4326)
        self._session.flush()
        return row

    def delete(self, mine_id: str) -> None:
        self._session.delete(self.get_required(mine_id))
        self._session.flush()
