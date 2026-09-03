"""数据源字典路由。"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.auth.dependencies import require_admin
from app.context import ActorContext
from app.data_sources.enums import DataSourceStatus
from app.data_sources.schemas import DataSourceCreate, DataSourceResponse, DataSourceUpdate
from app.data_sources.service import DataSourceService
from app.db import session_scope
from app.imagery.enums import RecordKind
from app.pagination import Page, PageParams
from app.query import BlankAsNone

router = APIRouter(prefix="/data-sources", tags=["数据源"])


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


def _service(session: Annotated[Session, Depends(_get_session)]) -> DataSourceService:
    return DataSourceService(session)


def _response(row: object) -> DataSourceResponse:
    return DataSourceResponse.model_validate(row, from_attributes=True)


@router.get("", summary="数据源列表", response_model=Page[DataSourceResponse])
def list_sources(
    params: Annotated[PageParams, Depends()],
    service: Annotated[DataSourceService, Depends(_service)],
    kind: Annotated[RecordKind | None, BlankAsNone, Query(description="按卫星/无人机过滤")] = None,
    status: Annotated[
        DataSourceStatus | None, BlankAsNone, Query(description="按是否启用过滤")
    ] = None,
    q: Annotated[str | None, BlankAsNone, Query(description="按编号或名称模糊查找")] = None,
) -> Page[DataSourceResponse]:
    page = service.list_sources(params, kind=kind, status=status, q=q)
    return Page.build([_response(item) for item in page.items], page.total, params)


@router.get("/{data_source_id}", summary="数据源详情", response_model=DataSourceResponse)
def get_source(
    data_source_id: Annotated[int, Path(description="数据源编号")],
    service: Annotated[DataSourceService, Depends(_service)],
) -> DataSourceResponse:
    return _response(service.get_required(data_source_id))


@router.post(
    "",
    status_code=201,
    summary="创建数据源",
    response_model=DataSourceResponse,
)
def create_source(
    body: DataSourceCreate,
    service: Annotated[DataSourceService, Depends(_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> DataSourceResponse:
    return _response(service.create(body))


@router.put("/{data_source_id}", summary="更新数据源", response_model=DataSourceResponse)
def update_source(
    data_source_id: Annotated[int, Path(description="数据源编号")],
    body: DataSourceUpdate,
    service: Annotated[DataSourceService, Depends(_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> DataSourceResponse:
    return _response(service.update(data_source_id, body))


@router.delete("/{data_source_id}", status_code=204, summary="删除数据源")
def delete_source(
    data_source_id: Annotated[int, Path(description="数据源编号")],
    service: Annotated[DataSourceService, Depends(_service)],
    _admin: Annotated[ActorContext, Depends(require_admin)],
) -> None:
    service.delete(data_source_id)
