"""资产路由：列表、详情、编辑、检索、下载、补 CRS、删除恢复。"""

import json
import re
from collections.abc import Iterator
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.assets.enums import AssetStatus, AssetType
from app.assets.geometry import GeometryValidationError, geojson_to_wkt
from app.assets.lifecycle import AssetLifecycleService
from app.assets.models import DataAsset
from app.assets.schemas import (
    AssetDetailResponse,
    AssetListItem,
    AssetUpdateRequest,
    BBox,
    DownloadUrlResponse,
    SearchItem,
    SearchRequest,
    SubmitInputRequest,
    SubmitInputResponse,
)
from app.assets.service import AssetService
from app.catalogs.models import Category
from app.context import get_actor
from app.db import session_scope
from app.errors import validation_error
from app.pagination import Page, PageParams
from app.settings import Settings
from app.uploads.minio import MinioAdapter

router = APIRouter(prefix="/assets", tags=["资产"])
_EPSG_PATTERN = re.compile(r"^EPSG:[1-9][0-9]{0,6}$", re.IGNORECASE)


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


def _service(session: Annotated[Session, Depends(_get_session)]) -> AssetService:
    return AssetService(session)


def _bbox_of(asset: DataAsset) -> BBox | None:
    if (
        asset.min_x is None
        or asset.min_y is None
        or asset.max_x is None
        or asset.max_y is None
    ):
        return None
    return BBox(min_x=asset.min_x, min_y=asset.min_y, max_x=asset.max_x, max_y=asset.max_y)


def _footprint_geojson(service: AssetService, geom: Any) -> dict[str, Any] | None:
    if geom is None:
        return None
    row = service._session.execute(sa.select(sa.func.ST_AsGeoJSON(geom))).scalar()
    return json.loads(row) if row else None


def _category_name(session: Session, category_id: int | None) -> str | None:
    if category_id is None:
        return None
    row = session.get(Category, category_id)
    return None if row is None else row.name


def _list_item(session: Session, asset: DataAsset) -> AssetListItem:
    return AssetListItem(
        id=asset.id,
        name=asset.name,
        asset_type=asset.asset_type,
        status=asset.status,
        category_id=asset.category_id,
        category_name=_category_name(session, asset.category_id),
        original_file_name=asset.original_file_name,
        size_bytes=asset.size_bytes,
        acquired_at=asset.acquired_at,
        created_at=asset.created_at,
        deleted_at=asset.deleted_at,
    )


def _detail(service: AssetService, asset: DataAsset) -> AssetDetailResponse:
    return AssetDetailResponse(
        **_list_item(service._session, asset).model_dump(),
        diagnostics=asset.diagnostics,
        crs=asset.crs,
        user_crs=asset.user_crs,
        width=asset.width,
        height=asset.height,
        band_count=asset.band_count,
        bbox=_bbox_of(asset),
        spatial_geojson=_footprint_geojson(service, asset.footprint),
        geometry_type=asset.geometry_type,
        feature_count=asset.feature_count,
        mime_type=asset.mime_type,
        has_map=asset.status is AssetStatus.READY
        and asset.asset_type is AssetType.RASTER
        and asset.cog_object_key is not None,
        has_download=asset.original_object_key is not None
        and asset.status is AssetStatus.READY,
    )


@router.get(
    "",
    summary="资产列表",
    description="管理页默认入口。deleted=true 只看回收站。",
    response_model=Page[AssetListItem],
)
def list_assets(
    service: Annotated[AssetService, Depends(_service)],
    pagination: Annotated[PageParams, Depends()],
    name: Annotated[str | None, Query(description="按名称模糊过滤")] = None,
    category_id: Annotated[int | None, Query(description="分类 ID")] = None,
    asset_type: Annotated[AssetType | None, Query(description="物理类型")] = None,
    status: Annotated[AssetStatus | None, Query(description="处理状态")] = None,
    deleted: Annotated[bool, Query(description="true 只列出回收站")] = False,
) -> Page[AssetListItem]:
    rows, total = service.list_assets(
        name=name,
        category_id=category_id,
        asset_type=asset_type,
        status=status,
        include_deleted=deleted,
        offset=pagination.offset,
        limit=pagination.limit,
    )
    return Page[AssetListItem](
        items=[_list_item(service._session, row) for row in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get(
    "/{asset_id}",
    summary="资产详情",
    response_model=AssetDetailResponse,
)
def get_asset(
    asset_id: Annotated[int, Path(description="资产 ID")],
    service: Annotated[AssetService, Depends(_service)],
) -> AssetDetailResponse:
    return _detail(service, service.get_asset_required(asset_id))


@router.patch(
    "/{asset_id}",
    summary="更新资产",
    description="改名称、分类、采集时间。未出现的字段保持不变；分类/采集时间传 null 表示清除。",
    response_model=AssetDetailResponse,
)
def update_asset(
    asset_id: Annotated[int, Path(description="资产 ID")],
    body: AssetUpdateRequest,
    service: Annotated[AssetService, Depends(_service)],
) -> AssetDetailResponse:
    get_actor()
    data = body.model_dump(exclude_unset=True)
    asset = service.update_asset(
        asset_id,
        name=data.get("name"),
        category_id=data.get("category_id"),
        acquired_at=data.get("acquired_at"),
        set_fields=set(data),
    )
    return _detail(service, asset)


@router.delete(
    "/{asset_id}",
    status_code=204,
    summary="删除资产",
    description="进入默认 7 天回收站。未完成的入库任务会请求取消。",
)
def delete_asset(
    asset_id: Annotated[int, Path(description="资产 ID")],
    request: Request,
    session: Annotated[Session, Depends(_get_session)],
) -> None:
    AssetLifecycleService(session).soft_delete(
        asset_id,
        retention_days=request.app.state.settings.asset_retention_days,
        actor=get_actor(),
    )


@router.post(
    "/{asset_id}/restore",
    summary="从回收站恢复",
    response_model=AssetDetailResponse,
)
def restore_asset(
    asset_id: Annotated[int, Path(description="资产 ID")],
    session: Annotated[Session, Depends(_get_session)],
) -> AssetDetailResponse:
    asset = AssetLifecycleService(session).restore(asset_id)
    return _detail(AssetService(session), asset)


@router.get(
    "/{asset_id}/download-url",
    summary="原件下载地址",
    response_model=DownloadUrlResponse,
)
def download_url(
    asset_id: Annotated[int, Path(description="资产 ID")],
    request: Request,
    service: Annotated[AssetService, Depends(_service)],
) -> DownloadUrlResponse:
    settings: Settings = request.app.state.settings
    asset = service.get_asset_required(asset_id)
    if asset.status is not AssetStatus.READY or not asset.original_object_key:
        raise validation_error(f"资产 {asset_id} 未就绪，不能下载")
    url = MinioAdapter(settings).presign_get_url(
        key=asset.original_object_key, expires_in=settings.download_expiry_seconds
    )
    return DownloadUrlResponse(url=url, expires_in_seconds=settings.download_expiry_seconds)


@router.post(
    "/search",
    summary="空间/条件检索",
    description="地图与监测选数。管理列表请用 GET /assets。",
    response_model=Page[SearchItem],
)
def search(
    body: SearchRequest,
    service: Annotated[AssetService, Depends(_service)],
) -> Page[SearchItem]:
    get_actor()
    try:
        geometry_wkt = (
            geojson_to_wkt(body.spatial_geojson) if body.spatial_geojson is not None else None
        )
    except GeometryValidationError as exc:
        raise validation_error(str(exc)) from exc
    rows, total = service.search_assets(
        geometry_wkt=geometry_wkt,
        asset_type=body.asset_type,
        status=body.status,
        acquired_from=body.acquired_from,
        acquired_to=body.acquired_to,
        category_id=body.category_id,
        ecological_parameter_ids=body.ecological_parameter_ids,
        offset=(body.page - 1) * body.page_size,
        limit=body.page_size,
    )
    items = [
        SearchItem(
            id=asset.id,
            name=asset.name,
            asset_type=asset.asset_type,
            status=asset.status,
            category_id=asset.category_id,
            category_name=_category_name(service._session, asset.category_id),
            acquired_at=asset.acquired_at,
            bbox=_bbox_of(asset) if body.spatial_geojson is not None else None,
        )
        for asset in rows
    ]
    return Page[SearchItem](items=items, total=total, page=body.page, page_size=body.page_size)


@router.post(
    "/{asset_id}/inputs",
    summary="补充坐标系并续跑",
    response_model=SubmitInputResponse,
)
def submit_input(
    asset_id: Annotated[int, Path(description="资产 ID")],
    body: SubmitInputRequest,
    service: Annotated[AssetService, Depends(_service)],
) -> SubmitInputResponse:
    asset = service.get_asset_required(asset_id)
    normalized_crs = body.crs.strip().upper()
    if _EPSG_PATTERN.fullmatch(normalized_crs) is None:
        raise validation_error(f"CRS 不合法：{body.crs!r}（应为 EPSG:4326 这类 EPSG 代码）")
    service.resume_from_needs_input(asset, user_crs=normalized_crs)
    return SubmitInputResponse(id=asset_id, status=AssetStatus.PROCESSING)
