"""矿山基础信息 CRUD。"""

import json
from collections.abc import Iterator
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.auth.dependencies import require_admin
from app.context import ActorContext
from app.db import session_scope
from app.errors import validation_error
from app.imagery.geometry import GeometryValidationError, geojson_to_wkt
from app.mines.models import Mine
from app.mines.schemas import MineCreate, MineResponse, MineUpdate
from app.mines.service import MineService
from app.pagination import Page, PageParams
from app.query import BlankAsNone

router = APIRouter(prefix="/mines", tags=["矿山"])


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


def _service(session: Annotated[Session, Depends(_get_session)]) -> MineService:
    return MineService(session)


def _spatial_geojson(session: Session, row: Mine) -> dict[str, Any]:
    raw = session.scalar(sa.select(sa.func.ST_AsGeoJSON(row.boundary_polygon)))
    if raw is None:
        raise RuntimeError(f"矿山 {row.mine_id} 缺少空间范围")
    return json.loads(raw)


def _response(session: Session, row: Mine) -> MineResponse:
    values = {column.name: getattr(row, column.name) for column in Mine.__table__.columns}
    values.pop("boundary_polygon")
    values["spatial_geojson"] = _spatial_geojson(session, row)
    return MineResponse.model_validate(values)


def _geometry_wkt(value: dict[str, Any]) -> str:
    try:
        return geojson_to_wkt(value)
    except GeometryValidationError as exc:
        raise validation_error(str(exc)) from exc


@router.get("", summary="矿山列表", response_model=Page[MineResponse])
def list_mines(
    params: Annotated[PageParams, Depends()],
    service: Annotated[MineService, Depends(_service)],
    q: Annotated[str | None, BlankAsNone, Query(description="按矿山编号或名称模糊查找")] = None,
    mine_province: Annotated[
        str | None, BlankAsNone, Query(description="按省份精确过滤")
    ] = None,
) -> Page[MineResponse]:
    page = service.list(params, q=q, mine_province=mine_province)
    return Page.build(
        [_response(service._session, item) for item in page.items], page.total, params
    )


@router.get("/{mine_id}", summary="矿山详情", response_model=MineResponse)
def get_mine(
    mine_id: Annotated[str, Path(description="矿山编号")],
    service: Annotated[MineService, Depends(_service)],
) -> MineResponse:
    return _response(service._session, service.get_required(mine_id))


@router.post("", status_code=201, summary="创建矿山", response_model=MineResponse)
def create_mine(
    body: MineCreate,
    service: Annotated[MineService, Depends(_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> MineResponse:
    row = service.create(body, _geometry_wkt(body.spatial_geojson))
    return _response(service._session, row)


@router.put("/{mine_id}", summary="更新矿山", response_model=MineResponse)
def update_mine(
    mine_id: Annotated[str, Path(description="矿山编号")],
    body: MineUpdate,
    service: Annotated[MineService, Depends(_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> MineResponse:
    wkt = None if body.spatial_geojson is None else _geometry_wkt(body.spatial_geojson)
    return _response(service._session, service.update(mine_id, body, wkt))


@router.delete("/{mine_id}", status_code=204, summary="删除矿山")
def delete_mine(
    mine_id: Annotated[str, Path(description="矿山编号")],
    service: Annotated[MineService, Depends(_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> None:
    service.delete(mine_id)
