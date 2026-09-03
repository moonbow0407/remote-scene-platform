"""数据源字典服务。"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.data_sources.enums import DataSourceStatus
from app.data_sources.models import DataSource
from app.data_sources.schemas import DataSourceCreate, DataSourceUpdate, resolve_kind
from app.errors import conflict, not_found, validation_error
from app.imagery.enums import RecordKind
from app.pagination import Page, PageParams

logger = logging.getLogger(__name__)


class DataSourceService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, data_source_id: int) -> DataSource | None:
        return self._session.get(DataSource, data_source_id)

    def get_required(self, data_source_id: int) -> DataSource:
        row = self.get(data_source_id)
        if row is None:
            raise not_found("数据源", data_source_id)
        return row

    def list_sources(
        self,
        params: PageParams,
        *,
        kind: RecordKind | None = None,
        status: DataSourceStatus | None = None,
        q: str | None = None,
    ) -> Page[DataSource]:
        stmt = sa.select(DataSource)
        count_stmt = sa.select(sa.func.count()).select_from(DataSource)
        if kind is not None:
            stmt = stmt.where(DataSource.kind == kind)
            count_stmt = count_stmt.where(DataSource.kind == kind)
        if status is not None:
            stmt = stmt.where(DataSource.status == status)
            count_stmt = count_stmt.where(DataSource.status == status)
        if q:
            pattern = f"%{q.strip()}%"
            filt = sa.or_(DataSource.code.ilike(pattern), DataSource.name.ilike(pattern))
            stmt = stmt.where(filt)
            count_stmt = count_stmt.where(filt)
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(DataSource.code).offset(params.offset).limit(params.limit)
            )
        )
        return Page.build(rows, total, params)

    def create(self, body: DataSourceCreate) -> DataSource:
        try:
            kind = resolve_kind(body.code, body.kind)
        except ValueError as exc:
            raise validation_error(str(exc)) from exc
        row = DataSource(code=body.code, name=body.name, kind=kind, status=body.status)
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="DATA_SOURCE_CODE_CONFLICT", detail=f"数据源编号 {body.code} 已存在"
            ) from exc
        return row

    def update(self, data_source_id: int, body: DataSourceUpdate) -> DataSource:
        row = self.get_required(data_source_id)
        data = body.model_dump(exclude_unset=True)
        if "name" in data:
            row.name = data["name"]
        if "status" in data:
            row.status = data["status"]
        self._session.flush()
        return row

    def delete(self, data_source_id: int) -> None:
        row = self.get_required(data_source_id)
        from app.ecology.models import EcologicalParameterDataSource
        from app.imagery.models import SatelliteData, UavData

        mapping_count = int(
            self._session.scalar(
                sa.select(sa.func.count())
                .select_from(EcologicalParameterDataSource)
                .where(EcologicalParameterDataSource.data_source_id == data_source_id)
            )
            or 0
        )
        if mapping_count:
            raise conflict(
                code="DATA_SOURCE_IN_USE",
                detail=f"数据源 {data_source_id} 仍被生态参量关系引用，禁止删除",
            )
        sat_count = int(
            self._session.scalar(
                sa.select(sa.func.count())
                .select_from(SatelliteData)
                .where(SatelliteData.data_source_id == data_source_id)
            )
            or 0
        )
        uav_count = int(
            self._session.scalar(
                sa.select(sa.func.count())
                .select_from(UavData)
                .where(UavData.data_source_id == data_source_id)
            )
            or 0
        )
        if sat_count or uav_count:
            raise conflict(
                code="DATA_SOURCE_IN_USE",
                detail=f"数据源 {data_source_id} 仍被卫星或无人机记录引用，禁止删除",
            )
        self._session.delete(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="DATA_SOURCE_IN_USE",
                detail=f"数据源 {data_source_id} 仍被其他业务关系引用，禁止删除",
            ) from exc
