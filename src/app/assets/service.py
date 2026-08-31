"""资产服务：创建、列表、检索、状态转换与元数据写入。

事务由调用方 session_scope 提交。处理步骤幂等：已有对象键则跳过。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from geoalchemy2 import WKTElement
from sqlalchemy.orm import Session

from app.assets.asset_state import is_asset_transition_allowed
from app.assets.enums import AssetStatus, AssetType
from app.assets.models import DataAsset
from app.catalogs.service import CatalogService
from app.context import ActorContext, get_actor
from app.errors import conflict, not_found

logger = logging.getLogger(__name__)


class AssetService:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create_asset(
        self,
        *,
        name: str,
        asset_type: AssetType,
        original_file_name: str,
        size_bytes: int,
        original_object_key: str | None = None,
        category_id: int | None = None,
        actor: ActorContext | None = None,
    ) -> DataAsset:
        actor = actor or get_actor()
        resolved_category = CatalogService(self._session).resolve_category_id(category_id)
        asset = DataAsset(
            name=name,
            asset_type=asset_type,
            status=AssetStatus.UPLOADING,
            original_file_name=original_file_name,
            size_bytes=size_bytes,
            original_object_key=original_object_key,
            category_id=resolved_category,
            created_by=None if actor.actor_id is None else int(actor.actor_id),
        )
        self._session.add(asset)
        self._session.flush()
        return asset

    def update_asset(
        self,
        asset_id: int,
        *,
        name: str | None = None,
        category_id: int | None = None,
        acquired_at: datetime | None = None,
        set_fields: set[str] | None = None,
    ) -> DataAsset:
        asset = self.get_asset_required(asset_id)
        assigned = set_fields or set()
        if name is not None:
            asset.name = name
        if "category_id" in assigned:
            asset.category_id = CatalogService(self._session).resolve_category_id(category_id)
        if "acquired_at" in assigned:
            asset.acquired_at = acquired_at
        self._session.flush()
        return asset

    def get_asset(self, asset_id: int, *, include_deleted: bool = False) -> DataAsset | None:
        asset = self._session.get(DataAsset, asset_id)
        if asset is not None and asset.deleted_at is not None and not include_deleted:
            return None
        return asset

    def get_asset_required(self, asset_id: int, *, include_deleted: bool = False) -> DataAsset:
        asset = self.get_asset(asset_id, include_deleted=include_deleted)
        if asset is None:
            raise not_found("资产", asset_id)
        return asset

    def get_asset_by_id(self, asset_id: int) -> DataAsset | None:
        """Worker 按主键取资产，不隐藏软删除行。"""
        return self._session.get(DataAsset, asset_id)

    def list_assets(
        self,
        *,
        name: str | None = None,
        category_id: int | None = None,
        asset_type: AssetType | None = None,
        status: AssetStatus | None = None,
        include_deleted: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[DataAsset], int]:
        stmt = sa.select(DataAsset)
        count_stmt = sa.select(sa.func.count()).select_from(DataAsset)
        conditions: list[sa.ColumnElement[bool]] = []
        if include_deleted:
            conditions.append(DataAsset.deleted_at.is_not(None))
        else:
            conditions.append(DataAsset.deleted_at.is_(None))
        if name:
            conditions.append(DataAsset.name.ilike(f"%{name.strip()}%"))
        if category_id is not None:
            CatalogService(self._session).get_required(category_id)
            conditions.append(DataAsset.category_id == category_id)
        if asset_type is not None:
            conditions.append(DataAsset.asset_type == asset_type)
        if status is not None:
            conditions.append(DataAsset.status == status)
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(DataAsset.created_at.desc()).offset(offset).limit(limit)
            )
        )
        return rows, total

    def set_status(
        self,
        asset: DataAsset,
        target: AssetStatus,
        *,
        diagnostics: dict[str, Any] | None = None,
    ) -> DataAsset:
        current = asset.status
        if current is target:
            if diagnostics is not None:
                asset.diagnostics = diagnostics
            return asset
        if not is_asset_transition_allowed(current, target):
            raise conflict(
                code="ASSET_STATE_INVALID",
                detail=f"资产 {asset.id} 不允许从 {current} 转换到 {target}",
            )
        asset.status = target
        if diagnostics is not None:
            asset.diagnostics = diagnostics
        return asset

    def mark_cancelled(self, asset_id: int, *, reason: str = "JOB_CANCELLED") -> None:
        asset = self.get_asset_by_id(asset_id)
        if asset is None or asset.status in (AssetStatus.READY, AssetStatus.FAILED):
            return
        if is_asset_transition_allowed(asset.status, AssetStatus.FAILED):
            self.set_status(
                asset,
                AssetStatus.FAILED,
                diagnostics={"reason": reason, "detail": "处理任务已取消"},
            )

    def update_fields(self, asset_id: int, **fields: Any) -> DataAsset:
        """幂等写入处理元数据；值为 None 的字段不覆盖。"""
        asset = self.get_asset_by_id(asset_id)
        if asset is None:
            raise not_found("资产", asset_id)
        for key, value in fields.items():
            if value is not None:
                setattr(asset, key, value)
        self._session.flush()
        return asset

    def search_assets(
        self,
        *,
        geometry_wkt: str | None = None,
        asset_type: AssetType | None = None,
        status: AssetStatus | None = None,
        acquired_from: datetime | None = None,
        acquired_to: datetime | None = None,
        category_id: int | None = None,
        ecological_parameter_ids: list[int] | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[DataAsset], int]:
        stmt = sa.select(DataAsset)
        count_stmt = sa.select(sa.func.count()).select_from(DataAsset)
        conditions: list[sa.ColumnElement[bool]] = [DataAsset.deleted_at.is_(None)]
        if geometry_wkt is not None:
            geom = WKTElement(geometry_wkt, srid=4326)
            conditions.append(sa.func.ST_Intersects(DataAsset.footprint, geom))
        if asset_type is not None:
            conditions.append(DataAsset.asset_type == asset_type)
        if status is not None:
            conditions.append(DataAsset.status == status)
        if acquired_from is not None:
            conditions.append(DataAsset.acquired_at >= acquired_from)
        if acquired_to is not None:
            conditions.append(DataAsset.acquired_at <= acquired_to)
        if category_id is not None:
            CatalogService(self._session).get_required(category_id)
            conditions.append(DataAsset.category_id == category_id)
        if ecological_parameter_ids:
            from app.ecology.service import EcologyService

            mapped_ids = EcologyService(self._session).mapped_category_ids(ecological_parameter_ids)
            if not mapped_ids:
                return [], 0
            conditions.append(DataAsset.category_id.in_(mapped_ids))
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)
        rows = list(
            self._session.scalars(
                stmt.order_by(DataAsset.created_at.desc()).offset(offset).limit(limit)
            )
        )
        total = int(self._session.scalar(count_stmt) or 0)
        return rows, total

    def resume_from_needs_input(self, asset: DataAsset, *, user_crs: str) -> None:
        from app.jobs.enums import JobStatus
        from app.jobs.models import Job
        from app.jobs.service import JobService

        locked = self._session.scalar(
            sa.select(DataAsset).where(DataAsset.id == asset.id).with_for_update()
        )
        if locked is None:
            raise not_found("资产", asset.id)
        if locked.status is not AssetStatus.NEEDS_INPUT:
            raise conflict(
                code="ASSET_NOT_NEEDS_INPUT",
                detail=f"资产 {locked.id} 不处于待补信息状态（当前 {locked.status}）",
            )
        job = self._session.scalars(
            sa.select(Job)
            .where(Job.asset_id == locked.id, Job.status == JobStatus.NEEDS_INPUT)
            .order_by(Job.created_at.desc())
            .with_for_update()
        ).first()
        if job is None:
            raise conflict(
                code="NEEDS_INPUT_JOB_MISSING",
                detail=f"资产 {locked.id} 待补信息，但没有可恢复的任务",
            )
        locked.user_crs = user_crs
        self.set_status(locked, AssetStatus.PROCESSING)
        JobService(self._session).requeue(job)
