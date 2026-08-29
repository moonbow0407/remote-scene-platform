"""目录模块服务：资源目录树、卫星与传感器用例。

边界：
- 本模块维护目录主数据，并提供子树 ID 供资产检索过滤；
- 删除时检查子节点与生态映射；资产等跨模块引用由 DB RESTRICT 在 flush 时拒绝，
  不跨模块写入对方表。
"""

from __future__ import annotations

import logging
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalogs.enums import CatalogStatus
from app.catalogs.models import ResourceCatalog, Satellite, Sensor
from app.catalogs.schemas import (
    ResourceCatalogCreate,
    ResourceCatalogTreeNode,
    ResourceCatalogUpdate,
    SatelliteCreate,
    SatelliteUpdate,
    SensorCreate,
    SensorUpdate,
)
from app.context import get_actor
from app.errors import conflict, not_found, validation_error
from app.ids import new_uuid7
from app.pagination import Page, PageParams

logger = logging.getLogger(__name__)


def _actor_uuid() -> UUID | None:
    actor = get_actor()
    if actor.actor_id is None:
        return None
    try:
        return UUID(actor.actor_id)
    except ValueError:
        return None


class CatalogService:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ----- Resource Catalog -----

    def get_resource(self, resource_id: UUID) -> ResourceCatalog | None:
        return self._session.get(ResourceCatalog, resource_id)

    def get_resource_required(self, resource_id: UUID) -> ResourceCatalog:
        row = self.get_resource(resource_id)
        if row is None:
            raise not_found("资源目录", resource_id)
        return row

    def list_resources(
        self,
        params: PageParams,
        *,
        status: CatalogStatus | None = None,
        parent_id: UUID | None = None,
        code: str | None = None,
        root_only: bool = False,
    ) -> Page[ResourceCatalog]:
        stmt = sa.select(ResourceCatalog)
        count_stmt = sa.select(sa.func.count()).select_from(ResourceCatalog)
        if status is not None:
            stmt = stmt.where(ResourceCatalog.status == status)
            count_stmt = count_stmt.where(ResourceCatalog.status == status)
        if code is not None:
            stmt = stmt.where(ResourceCatalog.code == code)
            count_stmt = count_stmt.where(ResourceCatalog.code == code)
        if root_only:
            stmt = stmt.where(ResourceCatalog.parent_id.is_(None))
            count_stmt = count_stmt.where(ResourceCatalog.parent_id.is_(None))
        elif parent_id is not None:
            stmt = stmt.where(ResourceCatalog.parent_id == parent_id)
            count_stmt = count_stmt.where(ResourceCatalog.parent_id == parent_id)
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(ResourceCatalog.sort_order, ResourceCatalog.code)
                .offset(params.offset)
                .limit(params.limit)
            )
        )
        return Page.build(rows, total, params)

    def create_resource(self, body: ResourceCatalogCreate) -> ResourceCatalog:
        self._ensure_resource_code_unique(body.code)
        if body.parent_id is not None:
            self.get_resource_required(body.parent_id)
        row = ResourceCatalog(
            id=new_uuid7(),
            code=body.code,
            name=body.name,
            parent_id=body.parent_id,
            status=body.status,
            sort_order=body.sort_order,
            created_by=_actor_uuid(),
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="RESOURCE_CATALOG_CODE_CONFLICT",
                detail=f"资源目录编码 {body.code} 已存在",
            ) from exc
        logger.info("创建资源目录", extra={"resource_id": str(row.id), "code": row.code})
        return row

    def update_resource(self, resource_id: UUID, body: ResourceCatalogUpdate) -> ResourceCatalog:
        row = self.get_resource_required(resource_id)
        data = body.model_dump(exclude_unset=True)

        if "code" in data and data["code"] != row.code:
            self._ensure_resource_code_unique(data["code"], exclude_id=resource_id)
            row.code = data["code"]
        if "name" in data:
            row.name = data["name"]
        if "status" in data:
            row.status = data["status"]
        if "sort_order" in data:
            row.sort_order = data["sort_order"]
        if "parent_id" in data:
            new_parent_id: UUID | None = data["parent_id"]
            if new_parent_id == resource_id:
                raise validation_error("不能将资源目录的父节点设为自己")
            if new_parent_id is not None:
                self.get_resource_required(new_parent_id)
                # 并发改父（A→B 与 B→A）各自做环检测时都看不到对方未提交的修改，
                # 可能双双通过后落库成环；用事务级 advisory lock 串行化父节点变更
                self._lock_catalog_tree_for_parent_change()
                if self._resource_would_cycle(resource_id, new_parent_id):
                    raise conflict(
                        code="RESOURCE_CATALOG_PARENT_CYCLE",
                        detail="更新父节点会形成目录环，已拒绝",
                    )
            row.parent_id = new_parent_id

        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="RESOURCE_CATALOG_CODE_CONFLICT",
                detail=f"资源目录编码 {row.code} 已存在",
            ) from exc
        return row

    def delete_resource(self, resource_id: UUID) -> None:
        row = self.get_resource_required(resource_id)
        child_count = self._session.scalar(
            sa.select(sa.func.count())
            .select_from(ResourceCatalog)
            .where(ResourceCatalog.parent_id == resource_id)
        )
        if int(child_count or 0) > 0:
            raise conflict(
                code="RESOURCE_CATALOG_HAS_CHILDREN",
                detail=f"资源目录 {resource_id} 仍有子节点，禁止删除",
            )
        if self._resource_has_ecology_mappings(resource_id):
            raise conflict(
                code="RESOURCE_CATALOG_IN_USE",
                detail=f"资源目录 {resource_id} 仍被生态参数映射引用，禁止删除",
            )
        self._session.delete(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="RESOURCE_CATALOG_IN_USE",
                detail=f"资源目录 {resource_id} 仍被其他业务关系引用，禁止删除",
            ) from exc

    def resource_tree(
        self, *, status: CatalogStatus | None = None
    ) -> list[ResourceCatalogTreeNode]:
        stmt = sa.select(ResourceCatalog).order_by(ResourceCatalog.sort_order, ResourceCatalog.code)
        if status is not None:
            stmt = stmt.where(ResourceCatalog.status == status)
        rows = list(self._session.scalars(stmt))
        by_parent: dict[UUID | None, list[ResourceCatalog]] = {}
        for row in rows:
            by_parent.setdefault(row.parent_id, []).append(row)

        def build(parent_id: UUID | None) -> list[ResourceCatalogTreeNode]:
            return [
                ResourceCatalogTreeNode(
                    id=item.id,
                    code=item.code,
                    name=item.name,
                    status=item.status,
                    sort_order=item.sort_order,
                    children=build(item.id),
                )
                for item in by_parent.get(parent_id, [])
            ]

        return build(None)

    def subtree_ids(self, resource_id: UUID) -> list[UUID]:
        """包含自身的子树主键；目录规模小，在内存中展开。

        visited 防御：数据因并发改父出现环时立即报数据不变量错误并终止遍历，
        绝不让检索请求无限循环拖垮进程。
        """
        self.get_resource_required(resource_id)
        rows = self._session.execute(sa.select(ResourceCatalog.id, ResourceCatalog.parent_id)).all()
        children: dict[UUID | None, list[UUID]] = {}
        for row_id, parent_id in rows:
            children.setdefault(parent_id, []).append(row_id)
        ordered: list[UUID] = []
        visited: set[UUID] = set()
        stack = [resource_id]
        while stack:
            current = stack.pop()
            if current in visited:
                raise RuntimeError(
                    "资源目录树出现环，数据不变量被破坏："
                    f"子树根 {resource_id} 的遍历重复经过节点 {current}；"
                    "请先修复 resource_catalog.parent_id 再执行检索"
                )
            visited.add(current)
            ordered.append(current)
            stack.extend(reversed(children.get(current, [])))
        return ordered

    def _ensure_resource_code_unique(self, code: str, *, exclude_id: UUID | None = None) -> None:
        stmt = sa.select(ResourceCatalog.id).where(ResourceCatalog.code == code)
        if exclude_id is not None:
            stmt = stmt.where(ResourceCatalog.id != exclude_id)
        if self._session.scalar(stmt) is not None:
            raise conflict(
                code="RESOURCE_CATALOG_CODE_CONFLICT",
                detail=f"资源目录编码 {code} 已存在",
            )

    def _lock_catalog_tree_for_parent_change(self) -> None:
        """以事务级 advisory lock 串行化父节点变更，防止并发改父互相看不到未提交修改而放过成环。

        仅 PostgreSQL 支持该函数；单元测试的 SQLite（无并发改父场景）直接跳过。
        """
        if self._session.get_bind().dialect.name != "postgresql":
            return
        self._session.execute(
            sa.text("SELECT pg_advisory_xact_lock(hashtext('catalog_parent_change'))")
        )

    def _resource_would_cycle(self, node_id: UUID, new_parent_id: UUID) -> bool:
        """从候选父节点向上走，若遇到 node_id 则成环。"""
        current: UUID | None = new_parent_id
        seen: set[UUID] = set()
        while current is not None:
            if current == node_id:
                return True
            if current in seen:
                return True
            seen.add(current)
            parent = self.get_resource(current)
            if parent is None:
                return False
            current = parent.parent_id
        return False

    def _resource_has_ecology_mappings(self, resource_id: UUID) -> bool:
        """探测生态映射引用（只读）。延迟导入避免 catalogs↔ecology 循环依赖。"""
        from app.ecology.models import EcologicalParameterResourceMapping

        return (
            self._session.scalar(
                sa.select(EcologicalParameterResourceMapping.id)
                .where(EcologicalParameterResourceMapping.resource_catalog_id == resource_id)
                .limit(1)
            )
            is not None
        )

    # ----- Satellite -----

    def get_satellite(self, satellite_id: UUID) -> Satellite | None:
        return self._session.get(Satellite, satellite_id)

    def get_satellite_required(self, satellite_id: UUID) -> Satellite:
        row = self.get_satellite(satellite_id)
        if row is None:
            raise not_found("卫星", satellite_id)
        return row

    def list_satellites(
        self,
        params: PageParams,
        *,
        status: CatalogStatus | None = None,
        code: str | None = None,
    ) -> Page[Satellite]:
        stmt = sa.select(Satellite)
        count_stmt = sa.select(sa.func.count()).select_from(Satellite)
        if status is not None:
            stmt = stmt.where(Satellite.status == status)
            count_stmt = count_stmt.where(Satellite.status == status)
        if code is not None:
            stmt = stmt.where(Satellite.code == code)
            count_stmt = count_stmt.where(Satellite.code == code)
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(Satellite.sort_order, Satellite.code)
                .offset(params.offset)
                .limit(params.limit)
            )
        )
        return Page.build(rows, total, params)

    def create_satellite(self, body: SatelliteCreate) -> Satellite:
        self._ensure_satellite_code_unique(body.code)
        row = Satellite(
            id=new_uuid7(),
            code=body.code,
            name=body.name,
            status=body.status,
            sort_order=body.sort_order,
            created_by=_actor_uuid(),
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="SATELLITE_CODE_CONFLICT", detail=f"卫星编码 {body.code} 已存在"
            ) from exc
        return row

    def update_satellite(self, satellite_id: UUID, body: SatelliteUpdate) -> Satellite:
        row = self.get_satellite_required(satellite_id)
        data = body.model_dump(exclude_unset=True)
        if "code" in data and data["code"] != row.code:
            self._ensure_satellite_code_unique(data["code"], exclude_id=satellite_id)
            row.code = data["code"]
        if "name" in data:
            row.name = data["name"]
        if "status" in data:
            row.status = data["status"]
        if "sort_order" in data:
            row.sort_order = data["sort_order"]
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="SATELLITE_CODE_CONFLICT", detail=f"卫星编码 {row.code} 已存在"
            ) from exc
        return row

    def delete_satellite(self, satellite_id: UUID) -> None:
        row = self.get_satellite_required(satellite_id)
        sensor_count = self._session.scalar(
            sa.select(sa.func.count())
            .select_from(Sensor)
            .where(Sensor.satellite_id == satellite_id)
        )
        if int(sensor_count or 0) > 0:
            raise conflict(
                code="SATELLITE_HAS_SENSORS",
                detail=f"卫星 {satellite_id} 仍被传感器引用，禁止删除",
            )
        self._session.delete(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="SATELLITE_IN_USE",
                detail=f"卫星 {satellite_id} 仍被其他业务关系引用，禁止删除",
            ) from exc

    def _ensure_satellite_code_unique(self, code: str, *, exclude_id: UUID | None = None) -> None:
        stmt = sa.select(Satellite.id).where(Satellite.code == code)
        if exclude_id is not None:
            stmt = stmt.where(Satellite.id != exclude_id)
        if self._session.scalar(stmt) is not None:
            raise conflict(code="SATELLITE_CODE_CONFLICT", detail=f"卫星编码 {code} 已存在")

    # ----- Sensor -----

    def get_sensor(self, sensor_id: UUID) -> Sensor | None:
        return self._session.get(Sensor, sensor_id)

    def get_sensor_required(self, sensor_id: UUID) -> Sensor:
        row = self.get_sensor(sensor_id)
        if row is None:
            raise not_found("传感器", sensor_id)
        return row

    def list_sensors(
        self,
        params: PageParams,
        *,
        status: CatalogStatus | None = None,
        satellite_id: UUID | None = None,
        code: str | None = None,
    ) -> Page[Sensor]:
        stmt = sa.select(Sensor)
        count_stmt = sa.select(sa.func.count()).select_from(Sensor)
        if status is not None:
            stmt = stmt.where(Sensor.status == status)
            count_stmt = count_stmt.where(Sensor.status == status)
        if satellite_id is not None:
            stmt = stmt.where(Sensor.satellite_id == satellite_id)
            count_stmt = count_stmt.where(Sensor.satellite_id == satellite_id)
        if code is not None:
            stmt = stmt.where(Sensor.code == code)
            count_stmt = count_stmt.where(Sensor.code == code)
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(Sensor.sort_order, Sensor.code)
                .offset(params.offset)
                .limit(params.limit)
            )
        )
        return Page.build(rows, total, params)

    def create_sensor(self, body: SensorCreate) -> Sensor:
        self.get_satellite_required(body.satellite_id)
        self._ensure_sensor_code_unique(body.code)
        row = Sensor(
            id=new_uuid7(),
            code=body.code,
            name=body.name,
            satellite_id=body.satellite_id,
            status=body.status,
            sort_order=body.sort_order,
            created_by=_actor_uuid(),
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="SENSOR_CODE_CONFLICT", detail=f"传感器编码 {body.code} 已存在"
            ) from exc
        return row

    def update_sensor(self, sensor_id: UUID, body: SensorUpdate) -> Sensor:
        row = self.get_sensor_required(sensor_id)
        data = body.model_dump(exclude_unset=True)
        if "satellite_id" in data and data["satellite_id"] != row.satellite_id:
            # 资产同时落 satellite_id 与 sensor_id，且要求 sensor 属于 satellite；
            # 变更已被资产引用的传感器所属卫星会让三个外键各自合法但业务上自相矛盾
            # （按卫星查得到、按传感器也查得到，而传感器已属于另一颗卫星），
            # 因此引用期间禁止变更，先解除资产引用再调整主数据。
            self.get_satellite_required(data["satellite_id"])
            if self._sensor_referenced_by_assets(sensor_id):
                raise conflict(
                    code="SENSOR_IN_USE",
                    detail=f"传感器 {sensor_id} 已被逻辑资产引用，禁止变更所属卫星",
                )
            row.satellite_id = data["satellite_id"]
        if "code" in data and data["code"] != row.code:
            self._ensure_sensor_code_unique(data["code"], exclude_id=sensor_id)
            row.code = data["code"]
        if "name" in data:
            row.name = data["name"]
        if "status" in data:
            row.status = data["status"]
        if "sort_order" in data:
            row.sort_order = data["sort_order"]
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="SENSOR_CODE_CONFLICT", detail=f"传感器编码 {row.code} 已存在"
            ) from exc
        return row

    def delete_sensor(self, sensor_id: UUID) -> None:
        row = self.get_sensor_required(sensor_id)
        self._session.delete(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="SENSOR_IN_USE",
                detail=f"传感器 {sensor_id} 仍被其他业务关系引用，禁止删除",
            ) from exc

    def _ensure_sensor_code_unique(self, code: str, *, exclude_id: UUID | None = None) -> None:
        stmt = sa.select(Sensor.id).where(Sensor.code == code)
        if exclude_id is not None:
            stmt = stmt.where(Sensor.id != exclude_id)
        if self._session.scalar(stmt) is not None:
            raise conflict(code="SENSOR_CODE_CONFLICT", detail=f"传感器编码 {code} 已存在")

    def _sensor_referenced_by_assets(self, sensor_id: UUID) -> bool:
        """探测逻辑资产对传感器的引用（只读）。延迟导入避免 catalogs↔assets 循环依赖。"""
        from app.assets.models import DataAsset

        return (
            self._session.scalar(
                sa.select(DataAsset.id).where(DataAsset.sensor_id == sensor_id).limit(1)
            )
            is not None
        )
