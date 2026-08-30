"""增量资产选择：按计划条件从 READY 资产版本中选出本次执行的输入。

边界说明：
- 空间相交、READY 状态、目录（含子树）与生态映射过滤复用 assets 模块公开的
  `AssetService.search_versions`，不在本模块复制其 SQL（AGENTS.md 模块边界）；
- 增量时间窗是监测业务的语义，在拿到候选后按窗口规则过滤，窗口定义见
  `is_in_window`；
- 候选分页拉全后统一过滤，而不是把窗口条件下推 SQL：首版数据量下可接受，把
  选择查询的索引与查询计划优化留到 Stage 6 的调度查询调优工作包。

增量窗口语义（与《阶段迁移实施方案》§8 "created or acquired after the previous
successful run" 一致）：候选版本的"数据时间"优先取 acquired_at；注册时间晚于
锚点的版本同样入选（晚注册的既有采集数据不能被永久跳过）。窗口下界是上一次
成功执行的 window_anchor（选择时刻），故任何版本至多被选中一次；无历史成功
执行（锚点为 None）时做全量选择，对应计划首次执行。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.assets.enums import AssetVersionStatus
from app.assets.models import AssetVersion
from app.assets.service import AssetService

# 分页拉取候选的页大小
_SELECTION_PAGE_SIZE = 500


@dataclass(frozen=True)
class SelectionCriteria:
    """一次执行的选择条件；boundary_wkt 为 EPSG:4326 MULTIPOLYGON WKT。

    boundary_wkt 允许为 None 仅用于无空间约束的单元测试场景与未来"不限空间"
    扩展；生产计划边界必填。
    """

    boundary_wkt: str | None
    resource_catalog_id: UUID | None
    ecological_parameter_ids: tuple[UUID, ...]
    # 增量窗口下界（UTC）：上一次成功执行的 window_anchor；None 表示全量
    window_anchor: datetime | None


def _as_utc(value: datetime) -> datetime:
    """统一比较时区。

    PostgreSQL timestamptz 经 psycopg 回读恒为 aware；naive 只会出现在 SQLite
    单元测试方言（其 DATETIME 丢弃 tzinfo，写入的即 UTC 值）。此处把 naive 按
    UTC 解释，避免测试方言与生产方言出现两套过滤逻辑。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def is_in_window(
    *, acquired_at: datetime | None, created_at: datetime, anchor: datetime | None
) -> bool:
    """增量窗口判定（纯函数）：版本是否属于锚点之后的新数据。"""
    if anchor is None:
        return True
    anchor_utc = _as_utc(anchor)
    if acquired_at is not None and _as_utc(acquired_at) > anchor_utc:
        return True
    return _as_utc(created_at) > anchor_utc


def select_ready_versions(session: Session, criteria: SelectionCriteria) -> list[AssetVersion]:
    """按条件选出本次执行的全部资产版本输入。

    只选择 READY 版本；VALIDATING/PROCESSING/NEEDS_INPUT/FAILED/DELETED 一律
    不进入输入快照（由 search_versions 的 version_status 条件保证）。
    """
    assets = AssetService(session)
    candidates: list[AssetVersion] = []
    offset = 0
    total: int | None = None
    while total is None or offset < total:
        page, total = assets.search_versions(
            geometry_wkt=criteria.boundary_wkt,
            version_status=AssetVersionStatus.READY,
            resource_catalog_id=criteria.resource_catalog_id,
            ecological_parameter_ids=list(criteria.ecological_parameter_ids) or None,
            offset=offset,
            limit=_SELECTION_PAGE_SIZE,
        )
        candidates.extend(version for version, _asset in page)
        if len(page) < _SELECTION_PAGE_SIZE:
            break
        offset += _SELECTION_PAGE_SIZE

    return [
        version
        for version in candidates
        if is_in_window(
            acquired_at=version.acquired_at,
            created_at=version.created_at,
            anchor=criteria.window_anchor,
        )
    ]
