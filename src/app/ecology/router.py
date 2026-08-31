"""生态模块路由：HTTP 适配层。"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.context import get_actor
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
    response_model=Page[EcologicalParameterResponse],
)
def list_parameters(
    params: Annotated[PageParams, Depends()],
    service: Annotated[EcologyService, Depends(_get_service)],
    status: Annotated[
        EcologicalParameterStatus | None,
        Query(description="启用状态：ACTIVE / DISABLED；省略不过滤"),
    ] = None,
    parent_id: Annotated[int | None, Query(description="父参数 ID；省略不过滤")] = None,
    code: Annotated[str | None, Query(max_length=64, description="业务编码精确匹配")] = None,
    root_only: Annotated[bool, Query(description="true 时只返回根节点")] = False,
) -> Page[EcologicalParameterResponse]:
    page = service.list_parameters(
        params, status=status, parent_id=parent_id, code=code, root_only=root_only
    )
    return Page.build([_parameter_response(item) for item in page.items], page.total, params)


@router.get(
    "/parameters/tree",
    summary="生态参数树",
    response_model=list[EcologicalParameterTreeNode],
)
def parameter_tree(
    service: Annotated[EcologyService, Depends(_get_service)],
    status: Annotated[
        EcologicalParameterStatus | None, Query(description="按启用状态过滤；省略返回全部")
    ] = None,
) -> list[EcologicalParameterTreeNode]:
    return service.parameter_tree(status=status)


@router.get(
    "/parameters/{parameter_id}",
    summary="生态参数详情",
    response_model=EcologicalParameterResponse,
)
def get_parameter(
    parameter_id: Annotated[int, Path(description="生态参数 ID")],
    service: Annotated[EcologyService, Depends(_get_service)],
) -> EcologicalParameterResponse:
    return _parameter_response(service.get_parameter_required(parameter_id))


@router.post(
    "/parameters",
    status_code=201,
    summary="创建生态参数",
    response_model=EcologicalParameterResponse,
)
def create_parameter(
    body: EcologicalParameterCreate, service: Annotated[EcologyService, Depends(_get_service)]
) -> EcologicalParameterResponse:
    get_actor()
    return _parameter_response(service.create_parameter(body))


@router.put(
    "/parameters/{parameter_id}",
    summary="更新生态参数",
    description="未出现的字段保持不变。parent_id 传 null 表示升为根节点。",
    response_model=EcologicalParameterResponse,
)
def update_parameter(
    parameter_id: Annotated[int, Path(description="生态参数 ID")],
    body: EcologicalParameterUpdate,
    service: Annotated[EcologyService, Depends(_get_service)],
) -> EcologicalParameterResponse:
    get_actor()
    return _parameter_response(service.update_parameter(parameter_id, body))


@router.delete(
    "/parameters/{parameter_id}",
    status_code=204,
    summary="删除生态参数",
    description="有子节点或仍被映射引用时会拒绝删除。",
)
def delete_parameter(
    parameter_id: Annotated[int, Path(description="生态参数 ID")],
    service: Annotated[EcologyService, Depends(_get_service)],
) -> None:
    get_actor()
    service.delete_parameter(parameter_id)


# ----- Mappings -----


@router.get(
    "/mappings",
    summary="生态映射列表",
    description="生态参数与资源目录的多对多关系。",
    response_model=Page[MappingResponse],
)
def list_mappings(
    params: Annotated[PageParams, Depends()],
    service: Annotated[EcologyService, Depends(_get_service)],
    ecological_parameter_id: Annotated[int | None, Query(description="按生态参数过滤")] = None,
    category_id: Annotated[int | None, Query(description="按分类过滤")] = None,
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
    description="已存在的关系幂等保留，不报冲突。",
    response_model=MappingBatchResponse,
)
def create_mappings_batch(
    body: MappingBatchCreate, service: Annotated[EcologyService, Depends(_get_service)]
) -> MappingBatchResponse:
    get_actor()
    return service.create_mappings_batch(body)


@router.get(
    "/mappings/{mapping_id}",
    summary="生态映射详情",
    response_model=MappingResponse,
)
def get_mapping(
    mapping_id: Annotated[int, Path(description="映射 ID")],
    service: Annotated[EcologyService, Depends(_get_service)],
) -> MappingResponse:
    return _mapping_response(service.get_mapping_required(mapping_id))


@router.post(
    "/mappings",
    status_code=201,
    summary="创建生态映射",
    description="将一个生态参数关联到一个资源目录节点。重复关系返回已有记录。",
    response_model=MappingResponse,
)
def create_mapping(
    body: MappingCreate, service: Annotated[EcologyService, Depends(_get_service)]
) -> MappingResponse:
    get_actor()
    return _mapping_response(service.create_mapping(body))


@router.put(
    "/mappings/{mapping_id}",
    summary="更新生态映射",
    response_model=MappingResponse,
)
def update_mapping(
    mapping_id: Annotated[int, Path(description="映射 ID")],
    body: MappingCreate,
    service: Annotated[EcologyService, Depends(_get_service)],
) -> MappingResponse:
    get_actor()
    return _mapping_response(service.update_mapping(mapping_id, body))


@router.delete(
    "/mappings/{mapping_id}",
    status_code=204,
    summary="删除生态映射",
)
def delete_mapping(
    mapping_id: Annotated[int, Path(description="映射 ID")],
    service: Annotated[EcologyService, Depends(_get_service)],
) -> None:
    get_actor()
    service.delete_mapping(mapping_id)
