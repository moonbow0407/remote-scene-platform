"""平铺分类路由。"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.catalogs.schemas import CategoryCreate, CategoryResponse, CategoryUpdate
from app.catalogs.service import CatalogService
from app.context import get_actor
from app.db import session_scope
from app.pagination import Page, PageParams

router = APIRouter(prefix="/categories", tags=["分类"])


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


def _get_service(session: Annotated[Session, Depends(_get_session)]) -> CatalogService:
    return CatalogService(session)


def _response(row: object) -> CategoryResponse:
    return CategoryResponse.model_validate(row, from_attributes=True)


@router.get(
    "",
    summary="分类列表",
    response_model=Page[CategoryResponse],
)
def list_categories(
    service: Annotated[CatalogService, Depends(_get_service)],
    pagination: Annotated[PageParams, Depends()],
    q: Annotated[str | None, Query(description="按名称模糊过滤")] = None,
) -> Page[CategoryResponse]:
    page = service.list_categories(pagination, q=q)
    return Page[CategoryResponse](
        items=[_response(row) for row in page.items],
        total=page.total,
        page=page.page,
        page_size=page.page_size,
    )


@router.get(
    "/{category_id}",
    summary="分类详情",
    response_model=CategoryResponse,
)
def get_category(
    category_id: Annotated[int, Path(description="分类 ID")],
    service: Annotated[CatalogService, Depends(_get_service)],
) -> CategoryResponse:
    return _response(service.get_required(category_id))


@router.post(
    "",
    status_code=201,
    summary="创建分类",
    response_model=CategoryResponse,
)
def create_category(
    body: CategoryCreate,
    service: Annotated[CatalogService, Depends(_get_service)],
) -> CategoryResponse:
    get_actor()
    return _response(service.create(body))


@router.put(
    "/{category_id}",
    summary="重命名分类",
    response_model=CategoryResponse,
)
def update_category(
    category_id: Annotated[int, Path(description="分类 ID")],
    body: CategoryUpdate,
    service: Annotated[CatalogService, Depends(_get_service)],
) -> CategoryResponse:
    get_actor()
    return _response(service.update(category_id, body))


@router.delete(
    "/{category_id}",
    status_code=204,
    summary="删除分类",
)
def delete_category(
    category_id: Annotated[int, Path(description="分类 ID")],
    service: Annotated[CatalogService, Depends(_get_service)],
) -> None:
    get_actor()
    service.delete(category_id)
