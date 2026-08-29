"""目录模块路由：HTTP 适配层。"""

from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app.catalogs.enums import CatalogStatus
from app.catalogs.schemas import (
    ResourceCatalogCreate,
    ResourceCatalogResponse,
    ResourceCatalogTreeNode,
    ResourceCatalogUpdate,
    SatelliteCreate,
    SatelliteResponse,
    SatelliteUpdate,
    SensorCreate,
    SensorResponse,
    SensorUpdate,
)
from app.catalogs.service import CatalogService
from app.context import get_actor
from app.db import session_scope
from app.pagination import Page, PageParams

router = APIRouter(prefix="/catalogs", tags=["目录"])


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


def _get_service(session: Annotated[Session, Depends(_get_session)]) -> CatalogService:
    return CatalogService(session)


def _resource_response(row: object) -> ResourceCatalogResponse:
    return ResourceCatalogResponse.model_validate(row, from_attributes=True)


def _satellite_response(row: object) -> SatelliteResponse:
    return SatelliteResponse.model_validate(row, from_attributes=True)


def _sensor_response(row: object) -> SensorResponse:
    return SensorResponse.model_validate(row, from_attributes=True)


# ----- Resource Catalog -----


@router.get("/resources", response_model=Page[ResourceCatalogResponse])
def list_resources(
    params: Annotated[PageParams, Depends()],
    service: Annotated[CatalogService, Depends(_get_service)],
    status: Annotated[CatalogStatus | None, Query()] = None,
    parent_id: Annotated[UUID | None, Query()] = None,
    code: Annotated[str | None, Query(max_length=64)] = None,
    root_only: Annotated[bool, Query(description="仅返回根节点")] = False,
) -> Page[ResourceCatalogResponse]:
    page = service.list_resources(
        params, status=status, parent_id=parent_id, code=code, root_only=root_only
    )
    return Page.build([_resource_response(item) for item in page.items], page.total, params)


@router.get("/resources/tree", response_model=list[ResourceCatalogTreeNode])
def resource_tree(
    service: Annotated[CatalogService, Depends(_get_service)],
    status: Annotated[CatalogStatus | None, Query()] = None,
) -> list[ResourceCatalogTreeNode]:
    return service.resource_tree(status=status)


@router.get("/resources/{resource_id}", response_model=ResourceCatalogResponse)
def get_resource(
    resource_id: UUID, service: Annotated[CatalogService, Depends(_get_service)]
) -> ResourceCatalogResponse:
    return _resource_response(service.get_resource_required(resource_id))


@router.post("/resources", status_code=201, response_model=ResourceCatalogResponse)
def create_resource(
    body: ResourceCatalogCreate, service: Annotated[CatalogService, Depends(_get_service)]
) -> ResourceCatalogResponse:
    get_actor()
    return _resource_response(service.create_resource(body))


@router.put("/resources/{resource_id}", response_model=ResourceCatalogResponse)
def update_resource(
    resource_id: UUID,
    body: ResourceCatalogUpdate,
    service: Annotated[CatalogService, Depends(_get_service)],
) -> ResourceCatalogResponse:
    get_actor()
    return _resource_response(service.update_resource(resource_id, body))


@router.delete("/resources/{resource_id}", status_code=204)
def delete_resource(
    resource_id: UUID, service: Annotated[CatalogService, Depends(_get_service)]
) -> None:
    get_actor()
    service.delete_resource(resource_id)


# ----- Satellite -----


@router.get("/satellites", response_model=Page[SatelliteResponse])
def list_satellites(
    params: Annotated[PageParams, Depends()],
    service: Annotated[CatalogService, Depends(_get_service)],
    status: Annotated[CatalogStatus | None, Query()] = None,
    code: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[SatelliteResponse]:
    page = service.list_satellites(params, status=status, code=code)
    return Page.build([_satellite_response(item) for item in page.items], page.total, params)


@router.get("/satellites/{satellite_id}/sensors", response_model=Page[SensorResponse])
def list_sensors_of_satellite(
    satellite_id: UUID,
    params: Annotated[PageParams, Depends()],
    service: Annotated[CatalogService, Depends(_get_service)],
    status: Annotated[CatalogStatus | None, Query()] = None,
) -> Page[SensorResponse]:
    service.get_satellite_required(satellite_id)
    page = service.list_sensors(params, satellite_id=satellite_id, status=status)
    return Page.build([_sensor_response(item) for item in page.items], page.total, params)


@router.get("/satellites/{satellite_id}", response_model=SatelliteResponse)
def get_satellite(
    satellite_id: UUID, service: Annotated[CatalogService, Depends(_get_service)]
) -> SatelliteResponse:
    return _satellite_response(service.get_satellite_required(satellite_id))


@router.post("/satellites", status_code=201, response_model=SatelliteResponse)
def create_satellite(
    body: SatelliteCreate, service: Annotated[CatalogService, Depends(_get_service)]
) -> SatelliteResponse:
    get_actor()
    return _satellite_response(service.create_satellite(body))


@router.put("/satellites/{satellite_id}", response_model=SatelliteResponse)
def update_satellite(
    satellite_id: UUID,
    body: SatelliteUpdate,
    service: Annotated[CatalogService, Depends(_get_service)],
) -> SatelliteResponse:
    get_actor()
    return _satellite_response(service.update_satellite(satellite_id, body))


@router.delete("/satellites/{satellite_id}", status_code=204)
def delete_satellite(
    satellite_id: UUID, service: Annotated[CatalogService, Depends(_get_service)]
) -> None:
    get_actor()
    service.delete_satellite(satellite_id)


# ----- Sensor -----


@router.get("/sensors", response_model=Page[SensorResponse])
def list_sensors(
    params: Annotated[PageParams, Depends()],
    service: Annotated[CatalogService, Depends(_get_service)],
    status: Annotated[CatalogStatus | None, Query()] = None,
    satellite_id: Annotated[UUID | None, Query()] = None,
    code: Annotated[str | None, Query(max_length=64)] = None,
) -> Page[SensorResponse]:
    page = service.list_sensors(params, status=status, satellite_id=satellite_id, code=code)
    return Page.build([_sensor_response(item) for item in page.items], page.total, params)


@router.get("/sensors/{sensor_id}", response_model=SensorResponse)
def get_sensor(
    sensor_id: UUID, service: Annotated[CatalogService, Depends(_get_service)]
) -> SensorResponse:
    return _sensor_response(service.get_sensor_required(sensor_id))


@router.post("/sensors", status_code=201, response_model=SensorResponse)
def create_sensor(
    body: SensorCreate, service: Annotated[CatalogService, Depends(_get_service)]
) -> SensorResponse:
    get_actor()
    return _sensor_response(service.create_sensor(body))


@router.put("/sensors/{sensor_id}", response_model=SensorResponse)
def update_sensor(
    sensor_id: UUID,
    body: SensorUpdate,
    service: Annotated[CatalogService, Depends(_get_service)],
) -> SensorResponse:
    get_actor()
    return _sensor_response(service.update_sensor(sensor_id, body))


@router.delete("/sensors/{sensor_id}", status_code=204)
def delete_sensor(
    sensor_id: UUID, service: Annotated[CatalogService, Depends(_get_service)]
) -> None:
    get_actor()
    service.delete_sensor(sensor_id)
