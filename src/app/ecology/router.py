"""生态模块路由：HTTP 适配层。"""

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
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


@router.get("/parameters", response_model=Page[EcologicalParameterResponse])
def list_parameters(
    params: Annotated[PageParams, Depends()],
    service: Annotated[EcologyService, Depends(_get_service)],
    status: Annotated[EcologicalParameterStatus | None, Query()] = None,
    parent_id: Annotated[UUID | None, Query()] = None,
    code: Annotated[str | None, Query(max_length=64)] = None,
    root_only: Annotated[bool, Query(description="仅返回根节点")] = False,
) -> Page[EcologicalParameterResponse]:
    page = service.list_parameters(
        params, status=status, parent_id=parent_id, code=code, root_only=root_only
    )
    return Page.build([_parameter_response(item) for item in page.items], page.total, params)


@router.get("/parameters/tree", response_model=list[EcologicalParameterTreeNode])
def parameter_tree(
    service: Annotated[EcologyService, Depends(_get_service)],
    status: Annotated[EcologicalParameterStatus | None, Query()] = None,
) -> list[EcologicalParameterTreeNode]:
    return service.parameter_tree(status=status)


@router.get("/parameters/{parameter_id}", response_model=EcologicalParameterResponse)
def get_parameter(
    parameter_id: UUID, service: Annotated[EcologyService, Depends(_get_service)]
) -> EcologicalParameterResponse:
    return _parameter_response(service.get_parameter_required(parameter_id))


@router.post("/parameters", status_code=201, response_model=EcologicalParameterResponse)
def create_parameter(
    body: EcologicalParameterCreate, service: Annotated[EcologyService, Depends(_get_service)]
) -> EcologicalParameterResponse:
    get_actor()
    return _parameter_response(service.create_parameter(body))


@router.put("/parameters/{parameter_id}", response_model=EcologicalParameterResponse)
def update_parameter(
    parameter_id: UUID,
    body: EcologicalParameterUpdate,
    service: Annotated[EcologyService, Depends(_get_service)],
) -> EcologicalParameterResponse:
    get_actor()
    return _parameter_response(service.update_parameter(parameter_id, body))


@router.delete("/parameters/{parameter_id}", status_code=204)
def delete_parameter(
    parameter_id: UUID, service: Annotated[EcologyService, Depends(_get_service)]
) -> None:
    get_actor()
    service.delete_parameter(parameter_id)


# ----- Mappings -----


@router.get("/mappings", response_model=Page[MappingResponse])
def list_mappings(
    params: Annotated[PageParams, Depends()],
    service: Annotated[EcologyService, Depends(_get_service)],
    ecological_parameter_id: Annotated[UUID | None, Query()] = None,
    resource_catalog_id: Annotated[UUID | None, Query()] = None,
) -> Page[MappingResponse]:
    page = service.list_mappings(
        params,
        ecological_parameter_id=ecological_parameter_id,
        resource_catalog_id=resource_catalog_id,
    )
    return Page.build([_mapping_response(item) for item in page.items], page.total, params)


@router.post("/mappings/batch", response_model=MappingBatchResponse)
def create_mappings_batch(
    body: MappingBatchCreate, service: Annotated[EcologyService, Depends(_get_service)]
) -> MappingBatchResponse:
    get_actor()
    return service.create_mappings_batch(body)


@router.get("/mappings/{mapping_id}", response_model=MappingResponse)
def get_mapping(
    mapping_id: UUID, service: Annotated[EcologyService, Depends(_get_service)]
) -> MappingResponse:
    return _mapping_response(service.get_mapping_required(mapping_id))


@router.post("/mappings", status_code=201, response_model=MappingResponse)
def create_mapping(
    body: MappingCreate, service: Annotated[EcologyService, Depends(_get_service)]
) -> MappingResponse:
    get_actor()
    return _mapping_response(service.create_mapping(body))


@router.delete("/mappings/{mapping_id}", status_code=204)
def delete_mapping(
    mapping_id: UUID, service: Annotated[EcologyService, Depends(_get_service)]
) -> None:
    get_actor()
    service.delete_mapping(mapping_id)
