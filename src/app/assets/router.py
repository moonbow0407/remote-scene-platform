"""资产路由：详情、版本、工件下载、空间检索与 NEEDS_INPUT 恢复。"""

import json
import re
from collections.abc import Iterator
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.assets.enums import ArtifactKind, AssetVersionStatus
from app.assets.geometry import GeometryValidationError, geojson_to_wkt
from app.assets.models import AssetVersion
from app.assets.schemas import (
    ArtifactResponse,
    AssetDetailResponse,
    BBox,
    RasterExtResponse,
    SearchItem,
    SearchRequest,
    SubmitInputRequest,
    VersionDetailResponse,
    VersionSummary,
)
from app.assets.service import AssetService
from app.context import get_actor
from app.db import session_scope
from app.errors import not_found, validation_error
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


def _version_summary(version: AssetVersion) -> VersionSummary:
    return VersionSummary(
        id=version.id,
        version_number=version.version_number,
        status=version.status,
        original_file_name=version.original_file_name,
        size_bytes=version.size_bytes,
        acquired_at=version.acquired_at,
        created_at=version.created_at,
    )


def _bbox_of(ext: Any) -> BBox | None:
    if ext is None or ext.min_x is None:
        return None
    return BBox(min_x=ext.min_x, min_y=ext.min_y, max_x=ext.max_x, max_y=ext.max_y)


@router.get("/{asset_id}", response_model=AssetDetailResponse)
def get_asset(
    asset_id: UUID, service: Annotated[AssetService, Depends(_service)]
) -> AssetDetailResponse:
    asset = service.get_asset_required(asset_id)
    current = (
        service.get_version_by_id(asset.current_version_id)
        if asset.current_version_id is not None
        else None
    )
    return AssetDetailResponse(
        id=asset.id,
        name=asset.name,
        asset_type=asset.asset_type,
        source=asset.source,
        properties=asset.properties,
        current_version=_version_summary(current) if current is not None else None,
        created_at=asset.created_at,
    )


@router.get("/{asset_id}/versions", response_model=Page[VersionSummary])
def list_versions(
    asset_id: UUID,
    service: Annotated[AssetService, Depends(_service)],
    pagination: Annotated[PageParams, Depends()],
) -> Page[VersionSummary]:
    service.get_asset_required(asset_id)
    session = service._session
    total = int(
        session.scalar(sa.select(sa.func.count()).where(AssetVersion.asset_id == asset_id)) or 0
    )
    rows = session.scalars(
        sa.select(AssetVersion)
        .where(AssetVersion.asset_id == asset_id)
        .order_by(AssetVersion.version_number.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    return Page[VersionSummary](
        items=[_version_summary(v) for v in rows],
        total=total,
        page=pagination.page,
        page_size=pagination.page_size,
    )


@router.get("/{asset_id}/versions/{version_id}", response_model=VersionDetailResponse)
def get_version(
    asset_id: UUID, version_id: UUID, service: Annotated[AssetService, Depends(_service)]
) -> VersionDetailResponse:
    version = service.get_version_required(asset_id, version_id)
    ext = service.get_raster_ext(version_id)
    raster = None
    if ext is not None:
        footprint_geojson = None
        if ext.footprint is not None:
            row = service._session.execute(sa.select(sa.func.ST_AsGeoJSON(ext.footprint))).scalar()
            footprint_geojson = json.loads(row) if row else None
        raster = RasterExtResponse(
            crs=ext.crs,
            user_crs=ext.user_crs,
            width=ext.width,
            height=ext.height,
            band_count=ext.band_count,
            bands=ext.bands,
            resolution_x=float(ext.resolution_x) if ext.resolution_x is not None else None,
            resolution_y=float(ext.resolution_y) if ext.resolution_y is not None else None,
            nodata=ext.nodata,
            render_profile=ext.render_profile,
            footprint_geojson=footprint_geojson,
            bbox=_bbox_of(ext),
        )
    artifacts = service.list_artifacts(version_id)
    return VersionDetailResponse(
        **_version_summary(version).model_dump(),
        properties=version.properties,
        diagnostics=version.diagnostics,
        raster=raster,
        artifacts=[
            ArtifactResponse(
                id=a.id,
                kind=a.kind.value,
                object_key=a.object_key,
                size_bytes=a.size_bytes,
                content_type=a.content_type,
            )
            for a in artifacts
        ],
    )


@router.get("/{asset_id}/versions/{version_id}/artifacts/{kind}/download-url")
def artifact_download_url(
    asset_id: UUID,
    version_id: UUID,
    kind: str,
    request: Request,
    service: Annotated[AssetService, Depends(_service)],
) -> dict[str, Any]:
    """短期签名下载 URL；MinIO 不直接暴露给客户端。"""
    settings: Settings = request.app.state.settings
    asset_kind = _artifact_kind(kind)
    version = service.get_version_required(asset_id, version_id)
    if version.status is not AssetVersionStatus.READY:
        raise validation_error(f"版本 {version_id} 未就绪，不能下载工件")
    artifact = service.get_artifact_required(version.id, asset_kind)
    minio = MinioAdapter(settings)
    url = minio.presign_get_url(
        key=artifact.object_key, expires_in=settings.download_expiry_seconds
    )
    return {
        "url": url,
        "expires_in_seconds": settings.download_expiry_seconds,
        "kind": asset_kind.value,
    }


@router.post("/search", response_model=Page[SearchItem])
def search(
    body: SearchRequest, service: Annotated[AssetService, Depends(_service)]
) -> Page[SearchItem]:
    """属性 + 空间联合检索；geometry 必须为 EPSG:4326 GeoJSON Polygon/MultiPolygon。"""
    get_actor()
    try:
        geometry_wkt = geojson_to_wkt(body.geometry) if body.geometry is not None else None
    except GeometryValidationError as exc:
        raise validation_error(str(exc)) from exc
    rows, total = service.search_versions(
        geometry_wkt=geometry_wkt,
        asset_type=body.asset_type,
        version_status=body.version_status,
        acquired_from=body.acquired_from,
        acquired_to=body.acquired_to,
        offset=(body.page - 1) * body.page_size,
        limit=body.page_size,
    )
    items = []
    for version, asset in rows:
        ext = service.get_raster_ext(version.id)
        items.append(
            SearchItem(
                asset_id=asset.id,
                asset_name=asset.name,
                asset_type=asset.asset_type,
                version_id=version.id,
                version_number=version.version_number,
                status=version.status,
                acquired_at=version.acquired_at,
                bbox=_bbox_of(ext),
            )
        )
    return Page[SearchItem](items=items, total=total, page=body.page, page_size=body.page_size)


@router.post("/{asset_id}/versions/{version_id}/inputs")
def submit_input(
    asset_id: UUID,
    version_id: UUID,
    body: SubmitInputRequest,
    service: Annotated[AssetService, Depends(_service)],
) -> dict[str, str]:
    """NEEDS_INPUT 恢复：补充 CRS 后从阻塞步骤继续，无需重新上传。"""
    version = service.get_version_required(asset_id, version_id)
    normalized_crs = body.crs.strip().upper()
    if _EPSG_PATTERN.fullmatch(normalized_crs) is None:
        raise validation_error(f"CRS 不合法：{body.crs!r}（应为 EPSG:4326 这类 EPSG 代码）")
    service.resume_from_needs_input(version, user_crs=normalized_crs)
    return {"asset_version_id": str(version_id), "status": AssetVersionStatus.PROCESSING.value}


def _artifact_kind(raw: str) -> ArtifactKind:
    try:
        return ArtifactKind(raw.upper())
    except ValueError as exc:
        raise not_found("工件", raw) from exc
