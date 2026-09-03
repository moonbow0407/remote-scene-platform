"""增量选择：按计划条件从 READY 卫星/无人机中选出本次执行的输入。"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.imagery.enums import RecordKind, RecordStatus
from app.imagery.service import ImageryService
from app.imagery.types import RasterRecord

_SELECTION_PAGE_SIZE = 500


@dataclass(frozen=True)
class SelectedRecord:
    kind: RecordKind
    record: RasterRecord


@dataclass(frozen=True)
class SelectionCriteria:
    boundary_wkt: str | None
    precision: str
    ecological_parameter_ids: tuple[int, ...]
    window_anchor: datetime | None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_in_window(
    *, acquired_at: datetime | None, created_at: datetime, anchor: datetime | None
) -> bool:
    if anchor is None:
        return True
    anchor_utc = _as_utc(anchor)
    if acquired_at is not None and _as_utc(acquired_at) > anchor_utc:
        return True
    return _as_utc(created_at) > anchor_utc


def select_ready_records(session: Session, criteria: SelectionCriteria) -> list[SelectedRecord]:
    imagery = ImageryService(session)
    candidates: list[SelectedRecord] = []
    offset = 0
    total: int | None = None
    while total is None or offset < total:
        page, total = imagery.search_records(
            geometry_wkt=criteria.boundary_wkt,
            status=RecordStatus.READY,
            ecological_parameter_ids=list(criteria.ecological_parameter_ids) or None,
            precision=criteria.precision if criteria.ecological_parameter_ids else None,
            offset=offset,
            limit=_SELECTION_PAGE_SIZE,
        )
        candidates.extend(SelectedRecord(kind=kind, record=row) for kind, row in page)
        if len(page) < _SELECTION_PAGE_SIZE:
            break
        offset += _SELECTION_PAGE_SIZE

    return [
        item
        for item in candidates
        if is_in_window(
            acquired_at=item.record.acquired_at,
            created_at=item.record.created_at,
            anchor=criteria.window_anchor,
        )
    ]
