"""资产路由：详情、版本、工件下载、空间检索与 NEEDS_INPUT 恢复。"""

import json
import re
from collections.abc import Iterator
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.orm import Session

from app.assets.enums import ArtifactKind, AssetVersionStatus
from app.assets.geometry import GeometryValidationError, geojson_to_wkt
from app.assets.lifecycle import AssetLifecycleService
from app.assets.models import AssetVersion
from app.assets.schemas import (
    ArtifactDownloadUrlResponse,
    ArtifactResponse,
    AssetDetailResponse,
    AssetUpdateRequest,
    AttachmentExtResponse,
    BBox,
    PropertySchemaItem,
    PropertySchemaUpsert,
    RasterExtResponse,
    SearchItem,
    SearchRequest,
    SubmitInputRequest,
    SubmitInputResponse,
    VectorExtResponse,
    VersionDetailResponse,
    VersionSummary,
)
from app.assets.service import AssetService
from app.catalogs.service import CatalogService
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


def _catalogs(session: Annotated[Session, Depends(_get_session)]) -> CatalogService:
    return CatalogService(session)


def _epsg_code(crs: str | None) -> int | None:
    if crs is None:
        return None
    text = crs.strip()
    if _EPSG_PATTERN.fullmatch(text) is None:
        return None
    return int(text.split(":", 1)[1])


def _classification(catalogs: CatalogService, asset: Any) -> dict[str, str | None]:
    catalog = (
        catalogs.get_resource(asset.resource_catalog_id) if asset.resource_catalog_id else None
    )
    satellite = catalogs.get_satellite(asset.satellite_id) if asset.satellite_id else None
    sensor = catalogs.get_sensor(asset.sensor_id) if asset.sensor_id else None
    return {
        "resource_catalog_code": None if catalog is None else catalog.code,
        "resource_catalog_name": None if catalog is None else catalog.name,
        "satellite_code": None if satellite is None else satellite.code,
        "satellite_name": None if satellite is None else satellite.name,
        "sensor_code": None if sensor is None else sensor.code,
        "sensor_name": None if sensor is None else sensor.name,
    }


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


def _footprint_geojson(service: AssetService, geom: Any) -> dict[str, Any] | None:
    if geom is None:
        return None
    row = service._session.execute(sa.select(sa.func.ST_AsGeoJSON(geom))).scalar()
    return json.loads(row) if row else None


@router.get(
    "/property-schemas",
    summary="列出属性模式",
    description="已登记的 JSON Schema，用于校验资产 properties。",
    response_model=list[PropertySchemaItem],
)
def list_property_schemas(
    service: Annotated[AssetService, Depends(_service)],
) -> list[PropertySchemaItem]:
    return [
        PropertySchemaItem(name=row.name, asset_type=row.asset_type, json_schema=row.schema)
        for row in service.list_property_schemas()
    ]


@router.put(
    "/property-schemas/{name}",
    summary="登记属性模式",
    description="按名称创建或覆盖 JSON Schema。写入资产 properties 前按此校验。",
    response_model=PropertySchemaItem,
)
def upsert_property_schema(
    name: Annotated[str, Path(description="属性模式名称")],
    body: PropertySchemaUpsert,
    service: Annotated[AssetService, Depends(_service)],
) -> PropertySchemaItem:
    row = service.register_property_schema(
        name=name, schema=body.json_schema, asset_type=body.asset_type
    )
    return PropertySchemaItem(name=row.name, asset_type=row.asset_type, json_schema=row.schema)


def _asset_detail(
    service: AssetService, catalogs: CatalogService, asset: Any
) -> AssetDetailResponse:
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
        resource_catalog_id=asset.resource_catalog_id,
        satellite_id=asset.satellite_id,
        sensor_id=asset.sensor_id,
        properties=asset.properties,
        current_version=_version_summary(current) if current is not None else None,
        created_at=asset.created_at,
        **_classification(catalogs, asset),
    )


@router.get(
    "/{asset_id}",
    summary="资产详情",
    description="返回逻辑资产及其当前版本摘要。软删除后普通查询不可见。",
    response_model=AssetDetailResponse,
)
def get_asset(
    asset_id: Annotated[UUID, Path(description="逻辑资产 ID")],
    service: Annotated[AssetService, Depends(_service)],
    catalogs: Annotated[CatalogService, Depends(_catalogs)],
) -> AssetDetailResponse:
    return _asset_detail(service, catalogs, service.get_asset_required(asset_id))


@router.patch(
    "/{asset_id}",
    summary="更新资产分类",
    description="部分更新名称与目录/卫星/传感器。未出现的字段保持不变；分类外键传 null 表示清除。",
    response_model=AssetDetailResponse,
)
def update_asset(
    asset_id: Annotated[UUID, Path(description="逻辑资产 ID")],
    body: AssetUpdateRequest,
    service: Annotated[AssetService, Depends(_service)],
    catalogs: Annotated[CatalogService, Depends(_catalogs)],
) -> AssetDetailResponse:
    get_actor()
    data = body.model_dump(exclude_unset=True)
    asset = service.update_asset(
        asset_id,
        name=data.get("name"),
        resource_catalog_id=data.get("resource_catalog_id"),
        satellite_id=data.get("satellite_id"),
        sensor_id=data.get("sensor_id"),
        set_fields=set(data),
    )
    return _asset_detail(service, catalogs, asset)


@router.delete(
    "/{asset_id}",
    status_code=204,
    summary="删除资产",
    description="软删除并进入默认 7 天恢复期。未完成的入库任务会同步请求取消。",
)
def delete_asset(
    asset_id: Annotated[UUID, Path(description="逻辑资产 ID")],
    request: Request,
    session: Annotated[Session, Depends(_get_session)],
) -> None:
    """软删除资产并启动恢复期；未完成入库任务同步请求取消。"""
    AssetLifecycleService(session).soft_delete(
        asset_id,
        retention_days=request.app.state.settings.asset_retention_days,
        actor=get_actor(),
    )


@router.post(
    "/{asset_id}/restore",
    summary="恢复资产",
    description="在恢复期内恢复软删除的逻辑资产。过期返回 409 ASSET_RESTORE_WINDOW_EXPIRED。",
    response_model=AssetDetailResponse,
)
def restore_asset(
    asset_id: Annotated[UUID, Path(description="逻辑资产 ID")],
    session: Annotated[Session, Depends(_get_session)],
) -> AssetDetailResponse:
    """在恢复期内恢复逻辑资产；过期后返回稳定的 409 problem。"""
    lifecycle = AssetLifecycleService(session)
    asset = lifecycle.restore(asset_id)
    return _asset_detail(AssetService(session), CatalogService(session), asset)


@router.get(
    "/{asset_id}/versions",
    summary="资产版本列表",
    description="按版本号倒序列出该资产的全部不可变版本。",
    response_model=Page[VersionSummary],
)
def list_versions(
    asset_id: Annotated[UUID, Path(description="逻辑资产 ID")],
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


@router.get(
    "/{asset_id}/versions/{version_id}",
    summary="版本详情",
    description=(
        "含栅格/矢量/附件扩展、覆盖范围、诊断信息和工件清单。NEEDS_INPUT 时看 diagnostics。"
    ),
    response_model=VersionDetailResponse,
)
def get_version(
    asset_id: Annotated[UUID, Path(description="逻辑资产 ID")],
    version_id: Annotated[UUID, Path(description="资产版本 ID")],
    request: Request,
    service: Annotated[AssetService, Depends(_service)],
) -> VersionDetailResponse:
    version = service.get_version_required(asset_id, version_id)
    ext = service.get_raster_ext(version_id)
    raster = None
    if ext is not None:
        raster_spatial = _footprint_geojson(service, ext.footprint)
        raster = RasterExtResponse(
            crs=ext.crs,
            user_crs=ext.user_crs,
            width=ext.width,
            height=ext.height,
            band_count=ext.band_count,
            bands=ext.bands,
            resolution_x=float(ext.resolution_x) if ext.resolution_x is not None else None,
            resolution_y=float(ext.resolution_y) if ext.resolution_y is not None else None,
            nodata_value=ext.nodata,
            render_profile=ext.render_profile,
            epsg_code=_epsg_code(ext.crs),
            spatial_geojson=raster_spatial,
            bbox=_bbox_of(ext),
        )
    vector_ext = service.get_vector_ext(version_id)
    vector = None
    if vector_ext is not None:
        vector_spatial = _footprint_geojson(service, vector_ext.footprint)
        vector = VectorExtResponse(
            crs=vector_ext.crs,
            user_crs=vector_ext.user_crs,
            geometry_type=vector_ext.geometry_type,
            feature_count=vector_ext.feature_count,
            native_format=vector_ext.native_format,
            property_schema=vector_ext.property_schema,
            epsg_code=_epsg_code(vector_ext.crs),
            spatial_geojson=vector_spatial,
            bbox=_bbox_of(vector_ext),
        )
    attachment_ext = service.get_attachment_ext(version_id)
    attachment = None
    if attachment_ext is not None:
        attachment = AttachmentExtResponse(
            mime_type=attachment_ext.mime_type,
            detected_format=attachment_ext.detected_format,
            original_file_name=attachment_ext.original_file_name,
        )
    artifacts = service.list_artifacts(version_id)
    bucket = request.app.state.settings.minio_bucket
    return VersionDetailResponse(
        **_version_summary(version).model_dump(),
        properties=version.properties,
        diagnostics=version.diagnostics,
        raster=raster,
        vector=vector,
        attachment=attachment,
        artifacts=[
            ArtifactResponse(
                id=a.id,
                kind=a.kind.value,
                bucket=bucket,
                object_key=a.object_key,
                size_bytes=a.size_bytes,
                content_type=a.content_type,
            )
            for a in artifacts
        ],
    )


@router.get(
    "/{asset_id}/versions/{version_id}/artifacts/{kind}/download-url",
    summary="工件下载地址",
    description=(
        "为 READY 版本签发短期签名下载 URL。"
        "kind 为 ORIGINAL / COG / THUMBNAIL。MinIO 不直接对客户端开放。"
    ),
    response_model=ArtifactDownloadUrlResponse,
)
def artifact_download_url(
    asset_id: Annotated[UUID, Path(description="逻辑资产 ID")],
    version_id: Annotated[UUID, Path(description="资产版本 ID")],
    kind: Annotated[str, Path(description="工件种类：ORIGINAL 原文件、COG、THUMBNAIL")],
    request: Request,
    service: Annotated[AssetService, Depends(_service)],
) -> ArtifactDownloadUrlResponse:
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
    return ArtifactDownloadUrlResponse(
        url=url,
        expires_in_seconds=settings.download_expiry_seconds,
        kind=asset_kind.value,
    )


@router.post(
    "/search",
    summary="检索资产",
    description=(
        "按空间范围、物理类型、采集时间、资源目录（含子树）、卫星、传感器、生态映射联合检索。"
        "空间几何必须是 EPSG:4326 的 Polygon 或 MultiPolygon。"
    ),
    response_model=Page[SearchItem],
)
def search(
    body: SearchRequest,
    service: Annotated[AssetService, Depends(_service)],
    catalogs: Annotated[CatalogService, Depends(_catalogs)],
) -> Page[SearchItem]:
    """属性 + 空间联合检索；spatial_geojson 必须为 EPSG:4326 GeoJSON Polygon/MultiPolygon。"""
    get_actor()
    try:
        geometry_wkt = (
            geojson_to_wkt(body.spatial_geojson) if body.spatial_geojson is not None else None
        )
    except GeometryValidationError as exc:
        raise validation_error(str(exc)) from exc
    rows, total = service.search_versions(
        geometry_wkt=geometry_wkt,
        asset_type=body.asset_type,
        version_status=body.version_status,
        acquired_from=body.acquired_from,
        acquired_to=body.acquired_to,
        resource_catalog_id=body.resource_catalog_id,
        satellite_id=body.satellite_id,
        sensor_id=body.sensor_id,
        ecological_parameter_ids=body.ecological_parameter_ids,
        offset=(body.page - 1) * body.page_size,
        limit=body.page_size,
    )
    items = []
    for version, asset in rows:
        ext = None
        if body.spatial_geojson is not None:
            # 空间检索才读 footprint 扩展；目录过滤不依赖栅格/矢量表。
            ext = service.get_raster_ext(version.id) or service.get_vector_ext(version.id)
        items.append(
            SearchItem(
                asset_id=asset.id,
                asset_name=asset.name,
                asset_type=asset.asset_type,
                version_id=version.id,
                version_number=version.version_number,
                status=version.status,
                acquired_at=version.acquired_at,
                resource_catalog_id=asset.resource_catalog_id,
                satellite_id=asset.satellite_id,
                sensor_id=asset.sensor_id,
                bbox=_bbox_of(ext),
                **_classification(catalogs, asset),
            )
        )
    return Page[SearchItem](items=items, total=total, page=body.page, page_size=body.page_size)


@router.post(
    "/{asset_id}/versions/{version_id}/inputs",
    summary="补充元数据并续跑",
    description=(
        "版本处于 NEEDS_INPUT（例如缺少 CRS）时，提交 EPSG 代码后从阻塞步骤继续，无需重新上传。"
    ),
    response_model=SubmitInputResponse,
)
def submit_input(
    asset_id: Annotated[UUID, Path(description="逻辑资产 ID")],
    version_id: Annotated[UUID, Path(description="资产版本 ID")],
    body: SubmitInputRequest,
    service: Annotated[AssetService, Depends(_service)],
) -> SubmitInputResponse:
    """NEEDS_INPUT 恢复：补充 CRS 后从阻塞步骤继续，无需重新上传。"""
    version = service.get_version_required(asset_id, version_id)
    normalized_crs = body.crs.strip().upper()
    if _EPSG_PATTERN.fullmatch(normalized_crs) is None:
        raise validation_error(f"CRS 不合法：{body.crs!r}（应为 EPSG:4326 这类 EPSG 代码）")
    service.resume_from_needs_input(version, user_crs=normalized_crs)
    return SubmitInputResponse(
        asset_version_id=str(version_id), status=AssetVersionStatus.PROCESSING.value
    )


def _artifact_kind(raw: str) -> ArtifactKind:
    try:
        return ArtifactKind(raw.upper())
    except ValueError as exc:
        raise not_found("工件", raw) from exc
