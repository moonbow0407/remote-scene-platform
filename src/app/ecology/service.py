"""生态参数与映射服务。

边界：
- 创建映射前通过 CatalogService 校验资源目录存在（模块间经公开 Service 协作）；
- 批量创建同事务、自动去重、已存在关系幂等返回；
- 更新映射是对两端 UUID 外键的原子替换，不以名称/code 作为关系键。
"""

from __future__ import annotations

import logging
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalogs.service import CatalogService
from app.context import get_actor
from app.ecology.enums import EcologicalParameterStatus
from app.ecology.models import EcologicalParameter, EcologicalParameterResourceMapping
from app.ecology.schemas import (
    EcologicalParameterCreate,
    EcologicalParameterTreeNode,
    EcologicalParameterUpdate,
    MappingBatchCreate,
    MappingBatchResponse,
    MappingCreate,
    MappingResponse,
)
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


class EcologyService:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ----- Ecological Parameter -----

    def get_parameter(self, parameter_id: UUID) -> EcologicalParameter | None:
        return self._session.get(EcologicalParameter, parameter_id)

    def get_parameter_required(self, parameter_id: UUID) -> EcologicalParameter:
        row = self.get_parameter(parameter_id)
        if row is None:
            raise not_found("生态参数", parameter_id)
        return row

    def list_parameters(
        self,
        params: PageParams,
        *,
        status: EcologicalParameterStatus | None = None,
        parent_id: UUID | None = None,
        code: str | None = None,
        root_only: bool = False,
    ) -> Page[EcologicalParameter]:
        stmt = sa.select(EcologicalParameter)
        count_stmt = sa.select(sa.func.count()).select_from(EcologicalParameter)
        if status is not None:
            stmt = stmt.where(EcologicalParameter.status == status)
            count_stmt = count_stmt.where(EcologicalParameter.status == status)
        if code is not None:
            stmt = stmt.where(EcologicalParameter.code == code)
            count_stmt = count_stmt.where(EcologicalParameter.code == code)
        if root_only:
            stmt = stmt.where(EcologicalParameter.parent_id.is_(None))
            count_stmt = count_stmt.where(EcologicalParameter.parent_id.is_(None))
        elif parent_id is not None:
            stmt = stmt.where(EcologicalParameter.parent_id == parent_id)
            count_stmt = count_stmt.where(EcologicalParameter.parent_id == parent_id)
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(EcologicalParameter.sort_order, EcologicalParameter.code)
                .offset(params.offset)
                .limit(params.limit)
            )
        )
        return Page.build(rows, total, params)

    def create_parameter(self, body: EcologicalParameterCreate) -> EcologicalParameter:
        self._ensure_parameter_code_unique(body.code)
        if body.parent_id is not None:
            self.get_parameter_required(body.parent_id)
        row = EcologicalParameter(
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
                code="ECOLOGICAL_PARAMETER_CODE_CONFLICT",
                detail=f"生态参数编码 {body.code} 已存在",
            ) from exc
        logger.info("创建生态参数", extra={"parameter_id": str(row.id), "code": row.code})
        return row

    def update_parameter(
        self, parameter_id: UUID, body: EcologicalParameterUpdate
    ) -> EcologicalParameter:
        row = self.get_parameter_required(parameter_id)
        data = body.model_dump(exclude_unset=True)

        if "code" in data and data["code"] != row.code:
            # 关系以 UUID 外键维系，允许改 code，但必须保持唯一
            self._ensure_parameter_code_unique(data["code"], exclude_id=parameter_id)
            row.code = data["code"]
        if "name" in data:
            row.name = data["name"]
        if "status" in data:
            row.status = data["status"]
        if "sort_order" in data:
            row.sort_order = data["sort_order"]
        if "parent_id" in data:
            new_parent_id: UUID | None = data["parent_id"]
            if new_parent_id == parameter_id:
                raise validation_error("不能将生态参数的父节点设为自己")
            if new_parent_id is not None:
                self.get_parameter_required(new_parent_id)
                if self._parameter_would_cycle(parameter_id, new_parent_id):
                    raise conflict(
                        code="ECOLOGICAL_PARAMETER_PARENT_CYCLE",
                        detail="更新父节点会形成参数环，已拒绝",
                    )
            row.parent_id = new_parent_id

        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="ECOLOGICAL_PARAMETER_CODE_CONFLICT",
                detail=f"生态参数编码 {row.code} 已存在",
            ) from exc
        return row

    def delete_parameter(self, parameter_id: UUID) -> None:
        row = self.get_parameter_required(parameter_id)
        child_count = self._session.scalar(
            sa.select(sa.func.count())
            .select_from(EcologicalParameter)
            .where(EcologicalParameter.parent_id == parameter_id)
        )
        if int(child_count or 0) > 0:
            raise conflict(
                code="ECOLOGICAL_PARAMETER_HAS_CHILDREN",
                detail=f"生态参数 {parameter_id} 仍有子节点，禁止删除",
            )
        mapping_count = self._session.scalar(
            sa.select(sa.func.count())
            .select_from(EcologicalParameterResourceMapping)
            .where(EcologicalParameterResourceMapping.ecological_parameter_id == parameter_id)
        )
        if int(mapping_count or 0) > 0:
            raise conflict(
                code="ECOLOGICAL_PARAMETER_IN_USE",
                detail=f"生态参数 {parameter_id} 仍被资源映射引用，禁止删除",
            )
        self._session.delete(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="ECOLOGICAL_PARAMETER_IN_USE",
                detail=f"生态参数 {parameter_id} 仍被其他业务关系引用，禁止删除",
            ) from exc

    def parameter_tree(
        self, *, status: EcologicalParameterStatus | None = None
    ) -> list[EcologicalParameterTreeNode]:
        stmt = sa.select(EcologicalParameter).order_by(
            EcologicalParameter.sort_order, EcologicalParameter.code
        )
        if status is not None:
            stmt = stmt.where(EcologicalParameter.status == status)
        rows = list(self._session.scalars(stmt))
        by_parent: dict[UUID | None, list[EcologicalParameter]] = {}
        for row in rows:
            by_parent.setdefault(row.parent_id, []).append(row)

        def build(parent_id: UUID | None) -> list[EcologicalParameterTreeNode]:
            return [
                EcologicalParameterTreeNode(
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

    def _ensure_parameter_code_unique(self, code: str, *, exclude_id: UUID | None = None) -> None:
        stmt = sa.select(EcologicalParameter.id).where(EcologicalParameter.code == code)
        if exclude_id is not None:
            stmt = stmt.where(EcologicalParameter.id != exclude_id)
        if self._session.scalar(stmt) is not None:
            raise conflict(
                code="ECOLOGICAL_PARAMETER_CODE_CONFLICT",
                detail=f"生态参数编码 {code} 已存在",
            )

    def _parameter_would_cycle(self, node_id: UUID, new_parent_id: UUID) -> bool:
        current: UUID | None = new_parent_id
        seen: set[UUID] = set()
        while current is not None:
            if current == node_id:
                return True
            if current in seen:
                return True
            seen.add(current)
            parent = self.get_parameter(current)
            if parent is None:
                return False
            current = parent.parent_id
        return False

    # ----- Mappings -----

    def get_mapping(self, mapping_id: UUID) -> EcologicalParameterResourceMapping | None:
        return self._session.get(EcologicalParameterResourceMapping, mapping_id)

    def get_mapping_required(self, mapping_id: UUID) -> EcologicalParameterResourceMapping:
        row = self.get_mapping(mapping_id)
        if row is None:
            raise not_found("生态资源映射", mapping_id)
        return row

    def list_mappings(
        self,
        params: PageParams,
        *,
        ecological_parameter_id: UUID | None = None,
        resource_catalog_id: UUID | None = None,
    ) -> Page[EcologicalParameterResourceMapping]:
        stmt = sa.select(EcologicalParameterResourceMapping)
        count_stmt = sa.select(sa.func.count()).select_from(EcologicalParameterResourceMapping)
        if ecological_parameter_id is not None:
            stmt = stmt.where(
                EcologicalParameterResourceMapping.ecological_parameter_id
                == ecological_parameter_id
            )
            count_stmt = count_stmt.where(
                EcologicalParameterResourceMapping.ecological_parameter_id
                == ecological_parameter_id
            )
        if resource_catalog_id is not None:
            stmt = stmt.where(
                EcologicalParameterResourceMapping.resource_catalog_id == resource_catalog_id
            )
            count_stmt = count_stmt.where(
                EcologicalParameterResourceMapping.resource_catalog_id == resource_catalog_id
            )
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(EcologicalParameterResourceMapping.created_at.desc())
                .offset(params.offset)
                .limit(params.limit)
            )
        )
        return Page.build(rows, total, params)

    def create_mapping(self, body: MappingCreate) -> EcologicalParameterResourceMapping:
        """单条创建：已存在则幂等返回已有行（与批量语义一致）。"""
        result = self.create_mappings_batch(MappingBatchCreate(items=[body]))
        if result.created:
            return self.get_mapping_required(result.created[0].id)
        return self.get_mapping_required(result.existing[0].id)

    def update_mapping(
        self, mapping_id: UUID, body: MappingCreate
    ) -> EcologicalParameterResourceMapping:
        """原子替换映射两端；目标组合已由其他行占用时返回稳定冲突。"""
        row = self.get_mapping_required(mapping_id)
        self.get_parameter_required(body.ecological_parameter_id)
        CatalogService(self._session).get_resource_required(body.resource_catalog_id)
        duplicate = self._session.scalar(
            sa.select(EcologicalParameterResourceMapping.id).where(
                EcologicalParameterResourceMapping.id != mapping_id,
                EcologicalParameterResourceMapping.ecological_parameter_id
                == body.ecological_parameter_id,
                EcologicalParameterResourceMapping.resource_catalog_id
                == body.resource_catalog_id,
            )
        )
        if duplicate is not None:
            raise conflict(
                code="ECOLOGY_MAPPING_CONFLICT",
                detail="目标生态参数与资源目录的映射已存在",
            )
        row.ecological_parameter_id = body.ecological_parameter_id
        row.resource_catalog_id = body.resource_catalog_id
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise conflict(
                code="ECOLOGY_MAPPING_CONFLICT",
                detail="目标生态参数与资源目录的映射已存在",
            ) from exc
        return row

    def create_mappings_batch(self, body: MappingBatchCreate) -> MappingBatchResponse:
        # 空输入安全：直接返回空结果
        if not body.items:
            return MappingBatchResponse(created=[], existing=[], created_count=0, existing_count=0)

        # 输入去重（保持首次出现顺序）
        unique_pairs: list[tuple[UUID, UUID]] = []
        seen_pairs: set[tuple[UUID, UUID]] = set()
        for item in body.items:
            pair = (item.ecological_parameter_id, item.resource_catalog_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            unique_pairs.append(pair)

        catalog_service = CatalogService(self._session)
        for parameter_id, resource_id in unique_pairs:
            self.get_parameter_required(parameter_id)
            catalog_service.get_resource_required(resource_id)

        pair_filters = [
            sa.and_(
                EcologicalParameterResourceMapping.ecological_parameter_id == parameter_id,
                EcologicalParameterResourceMapping.resource_catalog_id == resource_id,
            )
            for parameter_id, resource_id in unique_pairs
        ]
        existing_rows = list(
            self._session.scalars(
                sa.select(EcologicalParameterResourceMapping).where(sa.or_(*pair_filters))
            )
        )
        existing_keys = {
            (row.ecological_parameter_id, row.resource_catalog_id): row for row in existing_rows
        }

        created_rows: list[EcologicalParameterResourceMapping] = []
        for parameter_id, resource_id in unique_pairs:
            if (parameter_id, resource_id) in existing_keys:
                continue
            row = EcologicalParameterResourceMapping(
                id=new_uuid7(),
                ecological_parameter_id=parameter_id,
                resource_catalog_id=resource_id,
            )
            self._session.add(row)
            created_rows.append(row)

        try:
            self._session.flush()
        except IntegrityError as exc:
            # 并发下可能撞唯一约束；事务由请求边界回滚，客户端可重试幂等批量
            raise conflict(
                code="ECOLOGY_MAPPING_CONFLICT",
                detail="映射写入冲突，请重试；已存在关系应使用幂等批量接口",
            ) from exc

        existing_out = [
            MappingResponse.model_validate(row, from_attributes=True) for row in existing_rows
        ]
        created_out = [
            MappingResponse.model_validate(row, from_attributes=True) for row in created_rows
        ]
        return MappingBatchResponse(
            created=created_out,
            existing=existing_out,
            created_count=len(created_out),
            existing_count=len(existing_out),
        )

    def mapped_resource_catalog_ids(self, parameter_ids: list[UUID]) -> list[UUID]:
        """返回生态参数集合映射到的资源目录主键；空映射返回空列表（禁止生成 IN ()）。"""
        unique: list[UUID] = []
        seen: set[UUID] = set()
        for parameter_id in parameter_ids:
            if parameter_id in seen:
                continue
            seen.add(parameter_id)
            unique.append(parameter_id)
            self.get_parameter_required(parameter_id)
        if not unique:
            return []
        rows = list(
            self._session.scalars(
                sa.select(EcologicalParameterResourceMapping.resource_catalog_id).where(
                    EcologicalParameterResourceMapping.ecological_parameter_id.in_(unique)
                )
            )
        )
        return list(dict.fromkeys(rows))

    def delete_mapping(self, mapping_id: UUID) -> None:
        row = self.get_mapping_required(mapping_id)
        self._session.delete(row)
        self._session.flush()
