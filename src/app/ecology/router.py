"""生态模块路由：HTTP 适配层。"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.auth.dependencies import require_admin
from app.context import ActorContext
from app.db import session_scope
from app.ecology.enums import EcologicalParameterStatus
from app.ecology.schemas import (
    EcologicalParameterCreate,
    EcologicalParameterResponse,
    EcologicalParameterTreeNode,
    EcologicalParameterUpdate,
    MappingBatchCreate,
    MappingBatchResponse,
    MappingCreate,
    MappingResponse,
)
from app.ecology.service import EcologyService
from app.pagination import Page, PageParams

router = APIRouter(prefix="/ecology", tags=["生态"])


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


def _get_service(session: Annotated[Session, Depends(_get_session)]) -> EcologyService:
    return EcologyService(session)


def _parameter_response(row: object) -> EcologicalParameterResponse:
    return EcologicalParameterResponse.model_validate(row, from_attributes=True)


def _mapping_response(row: object) -> MappingResponse:
    return MappingResponse.model_validate(row, from_attributes=True)


# ----- Ecological Parameter -----


@router.get(
    "/parameters",
    summary="生态参数列表",
    description="平铺列出生态参数，可用状态、父节点、编码过滤。",
    response_model=Page[EcologicalParameterResponse],
)
def list_parameters(
    params: Annotated[PageParams, Depends()],
    service: Annotated[EcologyService, Depends(_get_service)],
    status: Annotated[
        EcologicalParameterStatus | None,
        Query(description="按是否启用过滤；不传则不限"),
    ] = None,
    parent_id: Annotated[int | None, Query(description="父参数编号；不传则不限")] = None,
    code: Annotated[str | None, Query(max_length=64, description="业务编码，精确匹配")] = None,
    root_only: Annotated[bool, Query(description="true 时只返回顶层参数")] = False,
) -> Page[EcologicalParameterResponse]:
    page = service.list_parameters(
        params, status=status, parent_id=parent_id, code=code, root_only=root_only
    )
    return Page.build([_parameter_response(item) for item in page.items], page.total, params)


@router.get(
    "/parameters/tree",
    summary="生态参数树",
    description="按父子关系嵌套返回，给树形选择器用。",
    response_model=list[EcologicalParameterTreeNode],
)
def parameter_tree(
    service: Annotated[EcologyService, Depends(_get_service)],
    status: Annotated[
        EcologicalParameterStatus | None, Query(description="按是否启用过滤；不传则返回全部")
    ] = None,
) -> list[EcologicalParameterTreeNode]:
    return service.parameter_tree(status=status)


@router.get(
    "/parameters/{parameter_id}",
    summary="生态参数详情",
    response_model=EcologicalParameterResponse,
)
def get_parameter(
    parameter_id: Annotated[int, Path(description="生态参数编号")],
    service: Annotated[EcologyService, Depends(_get_service)],
) -> EcologicalParameterResponse:
    return _parameter_response(service.get_parameter_required(parameter_id))


@router.post(
    "/parameters",
    status_code=201,
    summary="创建生态参数",
    description="编码全局不能重复。不填父参数就是顶层。",
    response_model=EcologicalParameterResponse,
)
def create_parameter(
    body: EcologicalParameterCreate,
    service: Annotated[EcologyService, Depends(_get_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> EcologicalParameterResponse:
    return _parameter_response(service.create_parameter(body))


@router.put(
    "/parameters/{parameter_id}",
    summary="更新生态参数",
    description="没写的字段保持原值。parent_id 传 null 表示升到顶层。",
    response_model=EcologicalParameterResponse,
)
def update_parameter(
    parameter_id: Annotated[int, Path(description="生态参数编号")],
    body: EcologicalParameterUpdate,
    service: Annotated[EcologyService, Depends(_get_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> EcologicalParameterResponse:
    return _parameter_response(service.update_parameter(parameter_id, body))


@router.delete(
    "/parameters/{parameter_id}",
    status_code=204,
    summary="删除生态参数",
    description="还有子参数，或仍有分类对应关系时，不能删除。",
)
def delete_parameter(
    parameter_id: Annotated[int, Path(description="生态参数编号")],
    service: Annotated[EcologyService, Depends(_get_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> None:
    service.delete_parameter(parameter_id)


# ----- Mappings -----


@router.get(
    "/mappings",
    summary="生态映射列表",
    description="生态参数和分类的对应关系。",
    response_model=Page[MappingResponse],
)
def list_mappings(
    params: Annotated[PageParams, Depends()],
    service: Annotated[EcologyService, Depends(_get_service)],
    ecological_parameter_id: Annotated[int | None, Query(description="按生态参数编号过滤")] = None,
    category_id: Annotated[int | None, Query(description="按分类编号过滤")] = None,
) -> Page[MappingResponse]:
    page = service.list_mappings(
        params,
        ecological_parameter_id=ecological_parameter_id,
        category_id=category_id,
    )
    return Page.build([_mapping_response(item) for item in page.items], page.total, params)


@router.post(
    "/mappings/batch",
    summary="批量创建生态映射",
    description="一次提交多条。已经存在的对应关系不会报错，会在 existing 里原样返回。",
    response_model=MappingBatchResponse,
)
def create_mappings_batch(
    body: MappingBatchCreate,
    service: Annotated[EcologyService, Depends(_get_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> MappingBatchResponse:
    return service.create_mappings_batch(body)


@router.get(
    "/mappings/{mapping_id}",
    summary="生态映射详情",
    response_model=MappingResponse,
)
def get_mapping(
    mapping_id: Annotated[int, Path(description="对应关系编号")],
    service: Annotated[EcologyService, Depends(_get_service)],
) -> MappingResponse:
    return _mapping_response(service.get_mapping_required(mapping_id))


@router.post(
    "/mappings",
    status_code=201,
    summary="创建生态映射",
    description="把一个生态参数对应到一个分类。已经存在时返回原记录，不报错。",
    response_model=MappingResponse,
)
def create_mapping(
    body: MappingCreate,
    service: Annotated[EcologyService, Depends(_get_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> MappingResponse:
    return _mapping_response(service.create_mapping(body))


@router.put(
    "/mappings/{mapping_id}",
    summary="更新生态映射",
    description="改这条对应关系所指向的生态参数或分类。",
    response_model=MappingResponse,
)
def update_mapping(
    mapping_id: Annotated[int, Path(description="对应关系编号")],
    body: MappingCreate,
    service: Annotated[EcologyService, Depends(_get_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> MappingResponse:
    return _mapping_response(service.update_mapping(mapping_id, body))


@router.delete(
    "/mappings/{mapping_id}",
    status_code=204,
    summary="删除生态映射",
    description="只删除这条对应关系，不删除生态参数或分类本身。",
)
def delete_mapping(
    mapping_id: Annotated[int, Path(description="对应关系编号")],
    service: Annotated[EcologyService, Depends(_get_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> None:
    service.delete_mapping(mapping_id)
