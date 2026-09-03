"""瓦片令牌校验（Nginx auth_request）。签发在卫星/无人机详情接口。"""

import logging

from fastapi import APIRouter, Request, Response

from app.tiles.service import extract_resource_from_uri, extract_token_from_uri, verify_tile_token

logger = logging.getLogger(__name__)

router = APIRouter(tags=["瓦片"])


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
