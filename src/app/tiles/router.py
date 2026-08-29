"""瓦片令牌路由：签发（面向前端）与校验（面向 Nginx auth_request 子请求）。"""

import logging
from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.assets.enums import ArtifactKind, AssetVersionStatus
from app.assets.service import AssetService
from app.db import session_scope
from app.errors import ProblemError, not_found
from app.settings import Settings
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


@router.get("/assets/{asset_id}/versions/{version_id}/tile-url")
def issue_tile_url(
    asset_id: UUID,
    version_id: UUID,
    request: Request,
    session: Annotated[Session, Depends(_get_session)],
) -> dict[str, object]:
    """为 READY 的栅格版本签发短期瓦片 URL 模板与令牌。"""
    settings: Settings = request.app.state.settings
    service = AssetService(session)
    version = service.get_version_required(asset_id, version_id)
    if version.status is not AssetVersionStatus.READY:
        raise not_found("可用瓦片", f"版本 {version_id} 未就绪")
    cog = service.get_artifact_required(version.id, ArtifactKind.COG)
    raster = service.get_raster_ext(version.id)
    render_profile = raster.render_profile if raster is not None else None
    raw_bands = render_profile.get("bands") if render_profile is not None else None
    if (
        raster is None
        or raster.band_count is None
        or not isinstance(raw_bands, list)
        or not raw_bands
        or any(
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 1
            or index > raster.band_count
            for index in raw_bands
        )
    ):
        raise ProblemError(
            status=500,
            code="RASTER_RENDER_PROFILE_INVALID",
            title="栅格渲染配置无效",
            detail=f"READY 版本 {version_id} 缺少合法的渲染波段配置",
        )
    resource = f"s3://{settings.minio_bucket}/{cog.object_key}"
    token, expires_at = sign_tile_token(
        version_id=str(version.id),
        resource=resource,
        ttl_seconds=settings.tile_token_ttl_seconds,
        secret=settings.tile_token_secret,
    )
    urls = build_tile_url_template(
        base_url=settings.public_base_url,
        cog_object_key=cog.object_key,
        bucket=settings.minio_bucket,
        token=token,
        band_indexes=raw_bands,
    )
    return {
        "asset_version_id": str(version.id),
        **urls,
        "token_expires_at": expires_at,
        "ttl_seconds": settings.tile_token_ttl_seconds,
    }


@router.get("/tiles/verify", include_in_schema=False)
def verify(request: Request) -> Response:
    """Nginx auth_request 子请求：从 X-Original-URI 提取并校验令牌。

    200 放行；401/403 拒绝。fail-closed：任何异常一律拒绝。
    """
    original_uri = request.headers.get("x-original-uri", "")
    try:
        token = extract_token_from_uri(original_uri)
        resource = extract_resource_from_uri(original_uri)
        version_id = verify_tile_token(
            token,
            resource=resource,
            secret=request.app.state.settings.tile_token_secret,
        )
        logger.debug("瓦片令牌校验通过", extra={"version_id": version_id})
        return Response(status_code=200)
    except Exception:
        # 系统边界统一拒绝：不向外部区分失败原因
        return Response(status_code=401)
