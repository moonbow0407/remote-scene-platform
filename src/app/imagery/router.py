"""卫星、无人机与统一检索路由。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.db import session_scope
from app.errors import ProblemError, validation_error
from app.imagery.enums import RecordKind, RecordStatus
from app.imagery.geometry import GeometryValidationError, geojson_to_wkt
from app.imagery.lifecycle import ImageryLifecycleService
from app.imagery.schemas import (
    BBox,
    DownloadUrlResponse,
    RecordDetailResponse,
    RecordListItem,
    RecordUpdateRequest,
    SearchItem,
    SearchRequest,
    SubmitInputRequest,
    SubmitInputResponse,
)
from app.imagery.service import ImageryService
from app.imagery.types import RECORD_LABEL, RasterRecord
from app.pagination import Page, PageParams
from app.query import BlankAsNone, blank_as_default
from app.settings import Settings
from app.tiles.schemas import TileUrlResponse
from app.tiles.service import build_tile_url_template, sign_tile_token
from app.uploads.minio import MinioAdapter

_EPSG_PATTERN = re.compile(r"^EPSG:[1-9][0-9]{0,6}$", re.IGNORECASE)

satellites_router = APIRouter(prefix="/satellites", tags=["卫星"])
uavs_router = APIRouter(prefix="/uavs", tags=["无人机"])
search_router = APIRouter(prefix="/data", tags=["检索"])


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


def _service(session: Annotated[Session, Depends(_get_session)]) -> ImageryService:
    return ImageryService(session)


def _bbox_of(row: RasterRecord) -> BBox | None:
    if row.min_x is None or row.min_y is None or row.max_x is None or row.max_y is None:
        return None
    return BBox(min_x=row.min_x, min_y=row.min_y, max_x=row.max_x, max_y=row.max_y)


def _footprint_geojson(session: Session, geom: Any) -> dict[str, Any] | None:
    if geom is None:
        return None
    raw = session.execute(sa.select(sa.func.ST_AsGeoJSON(geom))).scalar()
    return json.loads(raw) if raw else None


def _list_item(
    kind: RecordKind, row: RasterRecord, source_code: str | None, source_name: str | None
) -> RecordListItem:
    return RecordListItem(
        id=row.id,
        kind=kind,
        name=row.name,
        data_source_id=row.data_source_id,
        data_source_code=source_code,
        data_source_name=source_name,
        status=row.status,
        original_file_name=row.original_file_name,
        size_bytes=row.size_bytes,
        acquired_at=row.acquired_at,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
    )


def _detail(
    session: Session,
    kind: RecordKind,
    row: RasterRecord,
    source_code: str | None,
    source_name: str | None,
) -> RecordDetailResponse:
    return RecordDetailResponse(
        **_list_item(kind, row, source_code, source_name).model_dump(),
        diagnostics=row.diagnostics,
        crs=row.crs,
        user_crs=row.user_crs,
        width=row.width,
        height=row.height,
        band_count=row.band_count,
        bbox=_bbox_of(row),
        spatial_geojson=_footprint_geojson(session, row.footprint),
        has_map=row.status is RecordStatus.READY and row.cog_object_key is not None,
        has_download=row.original_object_key is not None and row.status is RecordStatus.READY,
    )


def _register_record_routes(router: APIRouter, kind: RecordKind) -> None:
    label = RECORD_LABEL[kind]

    @router.get("", summary=f"{label}列表", response_model=Page[RecordListItem])
    def list_records(
        service: Annotated[ImageryService, Depends(_service)],
        pagination: Annotated[PageParams, Depends()],
        name: Annotated[str | None, BlankAsNone, Query(description="按显示名称模糊查找")] = None,
        data_source_id: Annotated[
            int | None, BlankAsNone, Query(description="产品型号编号")
        ] = None,
        status: Annotated[RecordStatus | None, BlankAsNone, Query(description="处理状态")] = None,
        deleted: Annotated[
            bool, blank_as_default(False), Query(description="true 只列出回收站")
        ] = False,
    ) -> Page[RecordListItem]:
        rows, total = service.list_records(
            kind,
            name=name,
            data_source_id=data_source_id,
            status=status,
            include_deleted=deleted,
            offset=pagination.offset,
            limit=pagination.limit,
        )
        sources = service.data_source_map(rows)
        items = [
            _list_item(
                kind,
                row,
                sources[row.data_source_id].code if row.data_source_id in sources else None,
                sources[row.data_source_id].name if row.data_source_id in sources else None,
            )
            for row in rows
        ]
        return Page[RecordListItem](
            items=items, total=total, page=pagination.page, page_size=pagination.page_size
        )

    @router.get("/{record_id}", summary=f"{label}详情", response_model=RecordDetailResponse)
    def get_record(
        record_id: Annotated[int, Path(description="记录编号")],
        service: Annotated[ImageryService, Depends(_service)],
    ) -> RecordDetailResponse:
        row = service.get_required(kind, record_id)
        sources = service.data_source_map([row])
        source = sources.get(row.data_source_id)
        return _detail(
            service._session,
            kind,
            row,
            None if source is None else source.code,
            None if source is None else source.name,
        )

    @router.patch("/{record_id}", summary=f"更新{label}", response_model=RecordDetailResponse)
    def update_record(
        record_id: Annotated[int, Path(description="记录编号")],
        body: RecordUpdateRequest,
        service: Annotated[ImageryService, Depends(_service)],
    ) -> RecordDetailResponse:
        data = body.model_dump(exclude_unset=True)
        row = service.update_record(
            kind,
            record_id,
            name=data.get("name"),
            acquired_at=data.get("acquired_at"),
            set_fields=set(data),
        )
        sources = service.data_source_map([row])
        source = sources.get(row.data_source_id)
        return _detail(
            service._session,
            kind,
            row,
            None if source is None else source.code,
            None if source is None else source.name,
        )

    @router.delete("/{record_id}", status_code=204, summary=f"删除{label}")
    def delete_record(
        record_id: Annotated[int, Path(description="记录编号")],
        request: Request,
        session: Annotated[Session, Depends(_get_session)],
    ) -> None:
        ImageryLifecycleService(session).soft_delete(
            kind,
            record_id,
            retention_days=request.app.state.settings.asset_retention_days,
        )

    @router.post(
        "/{record_id}/restore",
        summary="从回收站恢复",
        response_model=RecordDetailResponse,
    )
    def restore_record(
        record_id: Annotated[int, Path(description="记录编号")],
        session: Annotated[Session, Depends(_get_session)],
    ) -> RecordDetailResponse:
        row = ImageryLifecycleService(session).restore(kind, record_id)
        service = ImageryService(session)
        sources = service.data_source_map([row])
        source = sources.get(row.data_source_id)
        return _detail(
            session,
            kind,
            row,
            None if source is None else source.code,
            None if source is None else source.name,
        )

    @router.get(
        "/{record_id}/download-url",
        summary="原件下载地址",
        response_model=DownloadUrlResponse,
    )
    def download_url(
        record_id: Annotated[int, Path(description="记录编号")],
        request: Request,
        service: Annotated[ImageryService, Depends(_service)],
    ) -> DownloadUrlResponse:
        settings: Settings = request.app.state.settings
        row = service.get_required(kind, record_id)
        if row.status is not RecordStatus.READY or not row.original_object_key:
            raise validation_error(f"{label} {record_id} 未就绪，不能下载")
        url = MinioAdapter(settings).presign_get_url(
            key=row.original_object_key, expires_in=settings.download_expiry_seconds
        )
        return DownloadUrlResponse(url=url, expires_in_seconds=settings.download_expiry_seconds)

    @router.post("/{record_id}/inputs", summary="补充坐标系", response_model=SubmitInputResponse)
    def submit_input(
        record_id: Annotated[int, Path(description="记录编号")],
        body: SubmitInputRequest,
        service: Annotated[ImageryService, Depends(_service)],
    ) -> SubmitInputResponse:
        row = service.get_required(kind, record_id)
        normalized_crs = body.crs.strip().upper()
        if _EPSG_PATTERN.fullmatch(normalized_crs) is None:
            raise validation_error(f"CRS 不合法：{body.crs!r}（应为 EPSG:4326 这类 EPSG 代码）")
        service.resume_from_needs_input(kind, row, user_crs=normalized_crs)
        return SubmitInputResponse(kind=kind, id=record_id, status=RecordStatus.PROCESSING)

    @router.get("/{record_id}/tile-url", summary="申请地图地址", response_model=TileUrlResponse)
    def issue_tile_url(
        record_id: Annotated[int, Path(description="记录编号")],
        request: Request,
        service: Annotated[ImageryService, Depends(_service)],
    ) -> TileUrlResponse:
        settings: Settings = request.app.state.settings
        row = service.get_required(kind, record_id)
        if row.status is not RecordStatus.READY or row.cog_object_key is None:
            raise validation_error(f"{label} {record_id} 未就绪，不能申请地图地址")
        render_profile = row.render_profile
        raw_bands = render_profile.get("bands") if render_profile is not None else None
        if (
            row.band_count is None
            or not isinstance(raw_bands, list)
            or not raw_bands
            or any(
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 1
                or index > row.band_count
                for index in raw_bands
            )
        ):
            raise ProblemError(
                status=500,
                code="RASTER_RENDER_PROFILE_INVALID",
                title="栅格渲染配置无效",
                detail=f"READY {label} {record_id} 缺少合法的渲染波段配置",
            )
        resource = f"s3://{settings.minio_bucket}/{row.cog_object_key}"
        token, expires_at = sign_tile_token(
            owner_ref=f"{kind.value}_{row.id}",
            resource=resource,
            ttl_seconds=settings.tile_token_ttl_seconds,
            secret=settings.tile_token_secret,
        )
        urls = build_tile_url_template(
            base_url=settings.public_base_url,
            cog_object_key=row.cog_object_key,
            bucket=settings.minio_bucket,
            token=token,
            band_indexes=raw_bands,
        )
        return TileUrlResponse(
            kind=kind,
            id=row.id,
            tile_url_template=str(urls["tile_url_template"]),
            tile_json_url=str(urls["tile_json_url"]),
            token_expires_at=expires_at,
            ttl_seconds=settings.tile_token_ttl_seconds,
        )


_register_record_routes(satellites_router, RecordKind.SATELLITE)
_register_record_routes(uavs_router, RecordKind.UAV)


@search_router.post("/search", summary="检索卫星与无人机", response_model=Page[SearchItem])
def search(
    body: SearchRequest,
    service: Annotated[ImageryService, Depends(_service)],
) -> Page[SearchItem]:
    try:
        geometry_wkt = (
            geojson_to_wkt(body.spatial_geojson) if body.spatial_geojson is not None else None
        )
    except GeometryValidationError as exc:
        raise validation_error(str(exc)) from exc
    rows, total = service.search_records(
        geometry_wkt=geometry_wkt,
        kind=body.kind,
        status=body.status,
        acquired_from=body.acquired_from,
        acquired_to=body.acquired_to,
        data_source_id=body.data_source_id,
        ecological_parameter_ids=body.ecological_parameter_ids or None,
        precision=None if body.precision is None else body.precision.value,
        offset=(body.page - 1) * body.page_size,
        limit=body.page_size,
    )
    sources = service.data_source_map([row for _, row in rows])
    items = []
    for item_kind, row in rows:
        source = sources.get(row.data_source_id)
        items.append(
            SearchItem(
                kind=item_kind,
                id=row.id,
                name=row.name,
                data_source_id=row.data_source_id,
                data_source_code=None if source is None else source.code,
                data_source_name=None if source is None else source.name,
                status=row.status,
                acquired_at=row.acquired_at,
                bbox=_bbox_of(row) if body.spatial_geojson is not None else None,
            )
        )
    return Page[SearchItem](items=items, total=total, page=body.page, page_size=body.page_size)
