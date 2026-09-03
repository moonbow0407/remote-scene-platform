"""卫星 / 无人机记录：创建、列表、检索、状态转换与元数据写入。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import WKTElement
from sqlalchemy.orm import Session

from app.data_sources.models import DataSource
from app.data_sources.service import DataSourceService
from app.errors import conflict, not_found, validation_error
from app.imagery.enums import RecordKind, RecordStatus
from app.imagery.models import SatelliteData, UavData
from app.imagery.record_state import is_record_transition_allowed
from app.imagery.types import RECORD_LABEL, RasterRecord, record_cls


class ImageryService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_record(
        self,
        *,
        kind: RecordKind,
        name: str,
        data_source_id: int,
        original_file_name: str,
        size_bytes: int,
        original_object_key: str | None = None,
    ) -> RasterRecord:
        source = DataSourceService(self._session).get_required(data_source_id)
        if source.kind is not kind:
            raise conflict(
                code="DATA_SOURCE_KIND_MISMATCH",
                detail=f"数据源 {source.code} 是 {source.kind}，不能用于 {kind}",
            )
        row = record_cls(kind)(
            name=name,
            data_source_id=data_source_id,
            status=RecordStatus.UPLOADING,
            original_file_name=original_file_name,
            size_bytes=size_bytes,
            original_object_key=original_object_key,
        )
        self._session.add(row)
        self._session.flush()
        return row

    def update_record(
        self,
        kind: RecordKind,
        record_id: int,
        *,
        name: str | None = None,
        acquired_at: datetime | None = None,
        set_fields: set[str] | None = None,
    ) -> RasterRecord:
        row = self.get_required(kind, record_id)
        assigned = set_fields or set()
        if name is not None:
            row.name = name
        if "acquired_at" in assigned:
            row.acquired_at = acquired_at
        self._session.flush()
        return row

    def get(self, kind: RecordKind, record_id: int) -> RasterRecord | None:
        return self._session.get(record_cls(kind), record_id)

    def get_required(self, kind: RecordKind, record_id: int) -> RasterRecord:
        row = self.get(kind, record_id)
        if row is None:
            raise not_found(RECORD_LABEL[kind], record_id)
        return row

    def get_by_id(self, kind: RecordKind, record_id: int) -> RasterRecord | None:
        return self._session.get(record_cls(kind), record_id)

    def get_from_job(self, *, owner_kind: str | None, owner_id: int | None) -> RasterRecord | None:
        if owner_kind is None or owner_id is None:
            return None
        return self.get_by_id(RecordKind(owner_kind), owner_id)

    def list_records(
        self,
        kind: RecordKind,
        *,
        name: str | None = None,
        data_source_id: int | None = None,
        status: RecordStatus | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[RasterRecord], int]:
        model = record_cls(kind)
        stmt = sa.select(model)
        count_stmt = sa.select(sa.func.count()).select_from(model)
        conditions: list[sa.ColumnElement[bool]] = []
        if name:
            conditions.append(model.name.ilike(f"%{name.strip()}%"))
        if data_source_id is not None:
            DataSourceService(self._session).get_required(data_source_id)
            conditions.append(model.data_source_id == data_source_id)
        if status is not None:
            conditions.append(model.status == status)
        if conditions:
            stmt = stmt.where(*conditions)
            count_stmt = count_stmt.where(*conditions)
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(stmt.order_by(model.created_at.desc()).offset(offset).limit(limit))
        )
        return rows, total

    def set_status(
        self,
        row: RasterRecord,
        target: RecordStatus,
        *,
        diagnostics: dict[str, Any] | None = None,
    ) -> RasterRecord:
        current = row.status
        if current is target:
            if diagnostics is not None:
                row.diagnostics = diagnostics
            return row
        if not is_record_transition_allowed(current, target):
            label = RECORD_LABEL[_kind_of(row)]
            raise conflict(
                code="RECORD_STATE_INVALID",
                detail=f"{label} {row.id} 不允许从 {current} 转换到 {target}",
            )
        row.status = target
        if diagnostics is not None:
            row.diagnostics = diagnostics
        return row

    def mark_cancelled(
        self, kind: RecordKind, record_id: int, *, reason: str = "JOB_CANCELLED"
    ) -> None:
        row = self.get_by_id(kind, record_id)
        if row is None or row.status in (RecordStatus.READY, RecordStatus.FAILED):
            return
        if is_record_transition_allowed(row.status, RecordStatus.FAILED):
            self.set_status(
                row,
                RecordStatus.FAILED,
                diagnostics={"reason": reason, "detail": "处理任务已取消"},
            )

    def update_fields(self, kind: RecordKind, record_id: int, **fields: Any) -> RasterRecord:
        row = self.get_by_id(kind, record_id)
        if row is None:
            raise not_found(RECORD_LABEL[kind], record_id)
        for key, value in fields.items():
            if value is not None:
                setattr(row, key, value)
        self._session.flush()
        return row

    def search_records(
        self,
        *,
        geometry_wkt: str | None = None,
        kind: RecordKind | None = None,
        status: RecordStatus | None = None,
        acquired_from: datetime | None = None,
        acquired_to: datetime | None = None,
        data_source_id: int | None = None,
        ecological_parameter_ids: list[int] | None = None,
        precision: str | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[tuple[RecordKind, RasterRecord]], int]:
        mapped_ids: list[int] | None = None
        if ecological_parameter_ids:
            from app.ecology.enums import Precision
            from app.ecology.service import EcologyService

            if precision is None:
                raise validation_error("按生态参量检索时必须传 precision（00 或 01）")
            mapped_ids = EcologyService(self._session).mapped_data_source_ids(
                ecological_parameter_ids, Precision(precision)
            )
            if not mapped_ids:
                return [], 0
            if data_source_id is not None and data_source_id not in mapped_ids:
                return [], 0
        source_ids = [data_source_id] if data_source_id is not None else mapped_ids
        if data_source_id is not None and mapped_ids is not None:
            source_ids = [data_source_id]

        kinds = [kind] if kind is not None else [RecordKind.SATELLITE, RecordKind.UAV]
        parts = []
        for item_kind in kinds:
            model = record_cls(item_kind)
            conditions = self._search_conditions(
                model,
                geometry_wkt=geometry_wkt,
                status=status,
                acquired_from=acquired_from,
                acquired_to=acquired_to,
                source_ids=source_ids,
            )
            parts.append(
                sa.select(
                    sa.literal(item_kind.value).label("kind"),
                    model.id.label("record_id"),
                    model.created_at.label("created_at"),
                ).where(*conditions)
            )
        union = sa.union_all(*parts).subquery()
        total = int(self._session.scalar(sa.select(sa.func.count()).select_from(union)) or 0)
        page_rows = list(
            self._session.execute(
                sa.select(union.c.kind, union.c.record_id)
                .order_by(union.c.created_at.desc())
                .offset(offset)
                .limit(limit)
            )
        )
        grouped: dict[RecordKind, list[int]] = {}
        order = [(RecordKind(kind_value), int(record_id)) for kind_value, record_id in page_rows]
        for item_kind, record_id in order:
            grouped.setdefault(item_kind, []).append(record_id)
        loaded: dict[tuple[RecordKind, int], RasterRecord] = {}
        for item_kind, ids in grouped.items():
            model = record_cls(item_kind)
            for row in self._session.scalars(sa.select(model).where(model.id.in_(ids))):
                loaded[(item_kind, row.id)] = row
        items = [(item_kind, loaded[(item_kind, record_id)]) for item_kind, record_id in order]
        return items, total

    def _search_conditions(
        self,
        model: type[RasterRecord],
        *,
        geometry_wkt: str | None,
        status: RecordStatus | None,
        acquired_from: datetime | None,
        acquired_to: datetime | None,
        source_ids: list[int] | None,
    ) -> list[sa.ColumnElement[bool]]:
        conditions: list[sa.ColumnElement[bool]] = []
        if geometry_wkt is not None:
            geom = WKTElement(geometry_wkt, srid=4326)
            conditions.append(sa.func.ST_Intersects(model.footprint, geom))
        if status is not None:
            conditions.append(model.status == status)
        if acquired_from is not None:
            conditions.append(model.acquired_at >= acquired_from)
        if acquired_to is not None:
            conditions.append(model.acquired_at <= acquired_to)
        if source_ids is not None:
            conditions.append(model.data_source_id.in_(source_ids))
        return conditions

    def data_source_map(self, rows: list[RasterRecord]) -> dict[int, DataSource]:
        ids = {row.data_source_id for row in rows}
        if not ids:
            return {}
        found = self._session.scalars(sa.select(DataSource).where(DataSource.id.in_(ids)))
        return {item.id: item for item in found}

    def resume_from_needs_input(
        self, kind: RecordKind, row: RasterRecord, *, user_crs: str
    ) -> None:
        from app.jobs.enums import JobStatus
        from app.jobs.models import Job
        from app.jobs.service import JobService

        model = record_cls(kind)
        locked = self._session.scalar(sa.select(model).where(model.id == row.id).with_for_update())
        if locked is None:
            raise not_found(RECORD_LABEL[kind], row.id)
        if locked.status is not RecordStatus.NEEDS_INPUT:
            raise conflict(
                code="RECORD_NOT_NEEDS_INPUT",
                detail=(
                    f"{RECORD_LABEL[kind]} {locked.id} 不处于待补信息状态"
                    f"（当前 {locked.status}）"
                ),
            )
        job = self._session.scalars(
            sa.select(Job)
            .where(
                Job.owner_kind == kind.value,
                Job.owner_id == locked.id,
                Job.status == JobStatus.NEEDS_INPUT,
            )
            .order_by(Job.created_at.desc())
            .with_for_update()
        ).first()
        if job is None:
            raise conflict(
                code="NEEDS_INPUT_JOB_MISSING",
                detail=f"{RECORD_LABEL[kind]} {locked.id} 待补信息，但没有可恢复的任务",
            )
        locked.user_crs = user_crs
        self.set_status(locked, RecordStatus.PROCESSING)
        JobService(self._session).requeue(job)


def _kind_of(row: RasterRecord) -> RecordKind:
    if isinstance(row, SatelliteData):
        return RecordKind.SATELLITE
    if isinstance(row, UavData):
        return RecordKind.UAV
    raise TypeError(type(row))
