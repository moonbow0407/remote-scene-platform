"""要素级空间检索。"""

from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.orm import Session

from app.assets.enums import AssetStatus, AssetType
from app.assets.geometry import GeometryValidationError, geojson_to_wkt
from app.assets.service import AssetService
from app.db import session_scope
from app.errors import validation_error
from app.pagination import Page
from app.vector_features.schemas import FeatureItem, FeatureSearchRequest
from app.vector_features.service import VectorFeatureService

router = APIRouter(prefix="/assets", tags=["矢量要素"])


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


@router.post(
    "/{asset_id}/features/search",
    summary="检索矢量要素",
    description="仅 READY 的矢量资产可用。范围必须是 EPSG:4326 的 Polygon 或 MultiPolygon。",
    response_model=Page[FeatureItem],
)
def search_features(
    asset_id: Annotated[int, Path(description="资产 ID")],
    body: FeatureSearchRequest,
    session: Annotated[Session, Depends(_get_session)],
) -> Page[FeatureItem]:
    assets = AssetService(session)
    asset = assets.get_asset_required(asset_id)
    if asset.status is not AssetStatus.READY:
        raise validation_error(f"资产 {asset_id} 未就绪，不能检索要素")
    if asset.asset_type is not AssetType.VECTOR:
        raise validation_error("只有矢量资产支持要素检索")
    try:
        geometry_wkt = geojson_to_wkt(body.spatial_geojson)
    except GeometryValidationError as exc:
        raise validation_error(str(exc)) from exc
    rows, total = VectorFeatureService(session).search(
        asset_id=asset.id,
        geometry_wkt=geometry_wkt,
        offset=(body.page - 1) * body.page_size,
        limit=body.page_size,
    )
    items = [
        FeatureItem(
            id=feature.id,
            asset_id=feature.asset_id,
            spatial_geojson=geojson,
            properties=feature.properties,
        )
        for feature, geojson in rows
    ]
    return Page[FeatureItem](items=items, total=total, page=body.page, page_size=body.page_size)
