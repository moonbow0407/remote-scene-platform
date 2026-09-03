"""生态参数与映射服务。

边界：
- 细项 code 为四位编号，大类由前两位推导；
- 创建映射前通过 CatalogService 校验分类存在；
- 批量创建同事务、自动去重、已存在关系幂等返回。
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.catalogs.service import CatalogService
from app.ecology.enums import EcologicalParameterStatus
from app.ecology.majors import MAJORS, major_code_of, resolve_major_name
from app.ecology.models import EcologicalParameter, EcologicalParameterResourceMapping
from app.ecology.schemas import (
    EcologicalParameterCreate,
    EcologicalParameterLeaf,
    EcologicalParameterMajorNode,
    EcologicalParameterUpdate,
    MajorResponse,
    MappingBatchCreate,
    MappingBatchResponse,
    MappingCreate,
    MappingResponse,
)
from app.errors import conflict, not_found
from app.pagination import Page, PageParams

logger = logging.getLogger(__name__)


def group_parameters_by_major(
    rows: list[EcologicalParameter],
) -> list[EcologicalParameterMajorNode]:
    grouped: dict[str, list[EcologicalParameter]] = {}
    names: dict[str, str] = {}
    for row in rows:
        grouped.setdefault(row.major_code, []).append(row)
        names[row.major_code] = row.major_name
    nodes: list[EcologicalParameterMajorNode] = []
    for major_code in sorted(grouped):
        children = [
            EcologicalParameterLeaf(
                id=item.id,
                code=item.code,
                abbrev=item.abbrev,
                name=item.name,
                english_name=item.english_name,
                status=item.status,
                sort_order=item.sort_order,
                remark=item.remark,
            )
            for item in grouped[major_code]
        ]
        nodes.append(
            EcologicalParameterMajorNode(
                major_code=major_code,
                major_name=names[major_code],
                children=children,
            )
        )
    return nodes


class EcologyService:
    def __init__(self, session: Session) -> None:
        self._session = session

    # ----- Ecological Parameter -----

    def get_parameter(self, parameter_id: int) -> EcologicalParameter | None:
        return self._session.get(EcologicalParameter, parameter_id)

    def get_parameter_required(self, parameter_id: int) -> EcologicalParameter:
        row = self.get_parameter(parameter_id)
        if row is None:
            raise not_found("生态参数", parameter_id)
        return row

    def list_parameters(
        self,
        params: PageParams,
        *,
        status: EcologicalParameterStatus | None = None,
        code: str | None = None,
        abbrev: str | None = None,
        major_code: str | None = None,
    ) -> Page[EcologicalParameter]:
        stmt = sa.select(EcologicalParameter)
        count_stmt = sa.select(sa.func.count()).select_from(EcologicalParameter)
        if status is not None:
            stmt = stmt.where(EcologicalParameter.status == status)
            count_stmt = count_stmt.where(EcologicalParameter.status == status)
        if code is not None:
            stmt = stmt.where(EcologicalParameter.code == code)
            count_stmt = count_stmt.where(EcologicalParameter.code == code)
        if abbrev is not None:
            stmt = stmt.where(EcologicalParameter.abbrev == abbrev)
            count_stmt = count_stmt.where(EcologicalParameter.abbrev == abbrev)
        if major_code is not None:
            stmt = stmt.where(EcologicalParameter.major_code == major_code)
            count_stmt = count_stmt.where(EcologicalParameter.major_code == major_code)
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(EcologicalParameter.code).offset(params.offset).limit(params.limit)
            )
        )
        return Page.build(rows, total, params)

    def list_majors(self) -> list[MajorResponse]:
        by_code = dict(MAJORS)
        extras = self._session.execute(
            sa.select(EcologicalParameter.major_code, EcologicalParameter.major_name)
            .where(EcologicalParameter.major_code.notin_(list(MAJORS)))
            .distinct()
        )
        for major_code, major_name in extras:
            by_code.setdefault(major_code, major_name)
        return [MajorResponse(code=code, name=name) for code, name in sorted(by_code.items())]

    def create_parameter(self, body: EcologicalParameterCreate) -> EcologicalParameter:
        major_code = major_code_of(body.code)
        major_name = resolve_major_name(major_code, body.major_name)
        self._ensure_parameter_code_unique(body.code)
        self._ensure_parameter_abbrev_unique(body.abbrev)
        row = EcologicalParameter(
            code=body.code,
            name=body.name,
            abbrev=body.abbrev,
            english_name=body.english_name,
            major_code=major_code,
            major_name=major_name,
            remark=body.remark,
            status=body.status,
            sort_order=body.sort_order,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            raise self._unique_conflict(exc, body.code, body.abbrev) from exc
        logger.info("创建生态参数", extra={"parameter_id": str(row.id), "code": row.code})
        return row

    def update_parameter(
        self, parameter_id: int, body: EcologicalParameterUpdate
    ) -> EcologicalParameter:
        row = self.get_parameter_required(parameter_id)
        data = body.model_dump(exclude_unset=True)

        if "code" in data and data["code"] != row.code:
            self._ensure_parameter_code_unique(data["code"], exclude_id=parameter_id)
            row.code = data["code"]
            row.major_code = major_code_of(row.code)
            row.major_name = resolve_major_name(row.major_code, data.get("major_name"))
        elif "major_name" in data:
            row.major_name = resolve_major_name(row.major_code, data["major_name"])
        if "name" in data:
            row.name = data["name"]
        if "abbrev" in data and data["abbrev"] != row.abbrev:
            self._ensure_parameter_abbrev_unique(data["abbrev"], exclude_id=parameter_id)
            row.abbrev = data["abbrev"]
        if "english_name" in data:
            row.english_name = data["english_name"]
        if "status" in data:
            row.status = data["status"]
        if "sort_order" in data:
            row.sort_order = data["sort_order"]
        if "remark" in data:
            row.remark = data["remark"]

        try:
            self._session.flush()
        except IntegrityError as exc:
            raise self._unique_conflict(exc, row.code, row.abbrev) from exc
        return row

    def delete_parameter(self, parameter_id: int) -> None:
        row = self.get_parameter_required(parameter_id)
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
    ) -> list[EcologicalParameterMajorNode]:
        stmt = sa.select(EcologicalParameter).order_by(EcologicalParameter.code)
        if status is not None:
            stmt = stmt.where(EcologicalParameter.status == status)
        return group_parameters_by_major(list(self._session.scalars(stmt)))

    def _ensure_parameter_code_unique(self, code: str, *, exclude_id: int | None = None) -> None:
        stmt = sa.select(EcologicalParameter.id).where(EcologicalParameter.code == code)
        if exclude_id is not None:
            stmt = stmt.where(EcologicalParameter.id != exclude_id)
        if self._session.scalar(stmt) is not None:
            raise conflict(
                code="ECOLOGICAL_PARAMETER_CODE_CONFLICT",
                detail=f"细项编号 {code} 已存在",
            )

    def _ensure_parameter_abbrev_unique(
        self, abbrev: str, *, exclude_id: int | None = None
    ) -> None:
        stmt = sa.select(EcologicalParameter.id).where(EcologicalParameter.abbrev == abbrev)
        if exclude_id is not None:
            stmt = stmt.where(EcologicalParameter.id != exclude_id)
        if self._session.scalar(stmt) is not None:
            raise conflict(
                code="ECOLOGICAL_PARAMETER_ABBREV_CONFLICT",
                detail=f"英文缩写 {abbrev} 已存在",
            )

    def _unique_conflict(self, exc: IntegrityError, code: str, abbrev: str) -> Exception:
        message = str(getattr(exc, "orig", exc))
        if "abbrev" in message.lower():
            return conflict(
                code="ECOLOGICAL_PARAMETER_ABBREV_CONFLICT",
                detail=f"英文缩写 {abbrev} 已存在",
            )
        return conflict(
            code="ECOLOGICAL_PARAMETER_CODE_CONFLICT",
            detail=f"细项编号 {code} 已存在",
        )

    # ----- Mappings -----

    def get_mapping(self, mapping_id: int) -> EcologicalParameterResourceMapping | None:
        return self._session.get(EcologicalParameterResourceMapping, mapping_id)

    def get_mapping_required(self, mapping_id: int) -> EcologicalParameterResourceMapping:
        row = self.get_mapping(mapping_id)
        if row is None:
            raise not_found("生态资源映射", mapping_id)
        return row

    def list_mappings(
        self,
        params: PageParams,
        *,
        ecological_parameter_id: int | None = None,
        category_id: int | None = None,
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
        if category_id is not None:
            stmt = stmt.where(EcologicalParameterResourceMapping.category_id == category_id)
            count_stmt = count_stmt.where(
                EcologicalParameterResourceMapping.category_id == category_id
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
        self, mapping_id: int, body: MappingCreate
    ) -> EcologicalParameterResourceMapping:
        """原子替换映射两端；目标组合已由其他行占用时返回稳定冲突。"""
        row = self.get_mapping_required(mapping_id)
        self.get_parameter_required(body.ecological_parameter_id)
        CatalogService(self._session).get_required(body.category_id)
        duplicate = self._session.scalar(
            sa.select(EcologicalParameterResourceMapping.id).where(
                EcologicalParameterResourceMapping.id != mapping_id,
                EcologicalParameterResourceMapping.ecological_parameter_id
                == body.ecological_parameter_id,
                EcologicalParameterResourceMapping.category_id == body.category_id,
            )
        )
        if duplicate is not None:
            raise conflict(
                code="ECOLOGY_MAPPING_CONFLICT",
                detail="目标生态参数与分类的映射已存在",
            )
        row.ecological_parameter_id = body.ecological_parameter_id
        row.category_id = body.category_id
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
        unique_pairs: list[tuple[int, int]] = []
        seen_pairs: set[tuple[int, int]] = set()
        for item in body.items:
            pair = (item.ecological_parameter_id, item.category_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            unique_pairs.append(pair)

        catalog_service = CatalogService(self._session)
        for parameter_id, resource_id in unique_pairs:
            self.get_parameter_required(parameter_id)
            catalog_service.get_required(resource_id)

        pair_filters = [
            sa.and_(
                EcologicalParameterResourceMapping.ecological_parameter_id == parameter_id,
                EcologicalParameterResourceMapping.category_id == resource_id,
            )
            for parameter_id, resource_id in unique_pairs
        ]
        existing_rows = list(
            self._session.scalars(
                sa.select(EcologicalParameterResourceMapping).where(sa.or_(*pair_filters))
            )
        )
        existing_keys = {
            (row.ecological_parameter_id, row.category_id): row for row in existing_rows
        }

        created_rows: list[EcologicalParameterResourceMapping] = []
        for parameter_id, resource_id in unique_pairs:
            if (parameter_id, resource_id) in existing_keys:
                continue
            row = EcologicalParameterResourceMapping(
                ecological_parameter_id=parameter_id,
                category_id=resource_id,
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

    def mapped_category_ids(self, parameter_ids: list[int]) -> list[int]:
        """返回生态参数集合映射到的分类主键；空映射返回空列表。"""
        unique: list[int] = []
        seen: set[int] = set()
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
                sa.select(EcologicalParameterResourceMapping.category_id).where(
                    EcologicalParameterResourceMapping.ecological_parameter_id.in_(unique)
                )
            )
        )
        return list(dict.fromkeys(rows))

    def delete_mapping(self, mapping_id: int) -> None:
        row = self.get_mapping_required(mapping_id)
        self._session.delete(row)
        self._session.flush()
