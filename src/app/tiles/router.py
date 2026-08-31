"""瓦片令牌路由：签发（面向前端）与校验（面向 Nginx auth_request 子请求）。"""

import logging
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from sqlalchemy.orm import Session

from app.assets.enums import AssetStatus, AssetType
from app.assets.service import AssetService
from app.db import session_scope
from app.errors import ProblemError, not_found
from app.settings import Settings
from app.tiles.schemas import TileUrlResponse
from app.tiles.service import (
    build_tile_url_template,
    extract_resource_from_uri,
    extract_token_from_uri,
    sign_tile_token,
    verify_tile_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["瓦片"])


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


@router.get(
    "/assets/{asset_id}/tile-url",
    summary="申请地图地址",
    description="只有处理完成的栅格可以申请。地址带短期令牌，过期后重新申请，不要改主机名。",
    response_model=TileUrlResponse,
)
def issue_tile_url(
    asset_id: Annotated[int, Path(description="资产编号")],
    request: Request,
    session: Annotated[Session, Depends(_get_session)],
) -> TileUrlResponse:
    settings: Settings = request.app.state.settings
    service = AssetService(session)
    asset = service.get_asset_required(asset_id)
    if asset.status is not AssetStatus.READY or asset.asset_type is not AssetType.RASTER:
        raise not_found("可用瓦片", f"资产 {asset_id} 未就绪")
    if asset.cog_object_key is None:
        raise not_found("可用瓦片", f"资产 {asset_id} 没有 COG")
    render_profile = asset.render_profile
    raw_bands = render_profile.get("bands") if render_profile is not None else None
    if (
        asset.band_count is None
        or not isinstance(raw_bands, list)
        or not raw_bands
        or any(
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 1
            or index > asset.band_count
            for index in raw_bands
        )
    ):
        raise ProblemError(
            status=500,
            code="RASTER_RENDER_PROFILE_INVALID",
            title="栅格渲染配置无效",
            detail=f"READY 资产 {asset_id} 缺少合法的渲染波段配置",
        )
    resource = f"s3://{settings.minio_bucket}/{asset.cog_object_key}"
    token, expires_at = sign_tile_token(
        asset_id=str(asset.id),
        resource=resource,
        ttl_seconds=settings.tile_token_ttl_seconds,
        secret=settings.tile_token_secret,
    )
    urls = build_tile_url_template(
        base_url=settings.public_base_url,
        cog_object_key=asset.cog_object_key,
        bucket=settings.minio_bucket,
        token=token,
        band_indexes=raw_bands,
    )
    return TileUrlResponse(
        asset_id=asset.id,
        tile_url_template=str(urls["tile_url_template"]),
        tile_json_url=str(urls["tile_json_url"]),
        token_expires_at=expires_at,
        ttl_seconds=settings.tile_token_ttl_seconds,
    )


@router.get("/tiles/verify", include_in_schema=False)
def verify(request: Request) -> Response:
    """Nginx auth_request：200 放行，任何异常一律 401。"""
    original_uri = request.headers.get("x-original-uri", "")
    try:
        token = extract_token_from_uri(original_uri)
        resource = extract_resource_from_uri(original_uri)
        verify_tile_token(
            token,
            resource=resource,
            secret=request.app.state.settings.tile_token_secret,
        )
        return Response(status_code=200)
    except Exception:
        return Response(status_code=401)
