"""增量资产选择：按计划条件从 READY 资产中选出本次执行的输入。"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.assets.enums import AssetStatus
from app.assets.models import DataAsset
from app.assets.service import AssetService

_SELECTION_PAGE_SIZE = 500


@dataclass(frozen=True)
class SelectionCriteria:
    boundary_wkt: str | None
    category_id: int | None
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


def select_ready_assets(session: Session, criteria: SelectionCriteria) -> list[DataAsset]:
    assets = AssetService(session)
    candidates: list[DataAsset] = []
    offset = 0
    total: int | None = None
    while total is None or offset < total:
        page, total = assets.search_assets(
            geometry_wkt=criteria.boundary_wkt,
            status=AssetStatus.READY,
            category_id=criteria.category_id,
            ecological_parameter_ids=list(criteria.ecological_parameter_ids) or None,
            offset=offset,
            limit=_SELECTION_PAGE_SIZE,
        )
        candidates.extend(page)
        if len(page) < _SELECTION_PAGE_SIZE:
            break
        offset += _SELECTION_PAGE_SIZE

    return [
        asset
        for asset in candidates
        if is_in_window(
            acquired_at=asset.acquired_at,
            created_at=asset.created_at,
            anchor=criteria.window_anchor,
        )
    ]
