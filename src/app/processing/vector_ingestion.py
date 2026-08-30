"""矢量入库：验证 → 哈希去重 → 读层 → 归一化到 EPSG:4326 → 导入要素 → 完成。

要素写入在同一数据库事务中先删后插，失败回滚后不会留下部分 live 要素。
"""

from __future__ import annotations

import logging
from typing import Any

from geoalchemy2 import WKTElement
from pyproj import CRS, Transformer
from shapely.ops import transform as shp_transform

from app.assets.enums import AssetVersionStatus
from app.assets.property_schema import infer_property_schema
from app.assets.service import AssetService
from app.db import session_scope
from app.ids import new_uuid7
from app.jobs.enums import JobStatus
from app.jobs.service import JobService
from app.processing.blob import ensure_source_local, hash_dedup_original, resolve_input_object
from app.processing.common import (
    IngestionContext,
    cancellation_checkpoint,
    cleanup_tmp_dir,
    preflight_tmp,
)
from app.processing.detect import DetectedKind, detect_file, sniff_head
from app.processing.errors import DeterministicError
from app.processing.vector_read import VectorLayer, read_vector_layer, shapely_to_wkt
from app.settings import Settings
from app.uploads.minio import MinioAdapter
from app.vector_features.models import VectorFeature
from app.vector_features.service import VectorFeatureService

logger = logging.getLogger(__name__)

_CONTENT_TYPES = {
    DetectedKind.GEOJSON: "application/geo+json",
    DetectedKind.SHAPEFILE_ZIP: "application/zip",
    DetectedKind.GEOPACKAGE: "application/geopackage+sqlite3",
}


class VectorIngestion:
    def __init__(self, *, settings: Settings, minio: MinioAdapter, engine: Any) -> None:
        self._settings = settings
        self._minio = minio
        self._engine = engine

    def run(self, ctx: IngestionContext) -> None:
        cleanup_tmp_dir(ctx.tmp_dir)
        try:
            preflight_tmp(ctx, self._settings)
            cancellation_checkpoint(ctx, self._engine)
            kind = self._step_validate(ctx)
            cancellation_checkpoint(ctx, self._engine)
            hash_dedup_original(
                minio=self._minio,
                engine=self._engine,
                ctx=ctx,
                content_type=_CONTENT_TYPES[kind],
            )
            cancellation_checkpoint(ctx, self._engine)
            self._step_import(ctx, kind)
            cancellation_checkpoint(ctx, self._engine)
            self._step_finalize(ctx)
        finally:
            cleanup_tmp_dir(ctx.tmp_dir)

    def _step_validate(self, ctx: IngestionContext) -> DetectedKind:
        # 输入对象按 resolve_input_object 统一解析：哈希去重删除上传源后，重试改用 canonical blob
        key, expected_size = resolve_input_object(engine=self._engine, ctx=ctx)
        stat = self._minio.head_object(key=key)
        if stat is None and key != ctx.source_object_key:
            key = ctx.source_object_key
            expected_size = ctx.source_size_bytes
            stat = self._minio.head_object(key=key)
        if stat is None:
            raise DeterministicError(
                "SOURCE_OBJECT_MISSING",
                f"输入对象不存在：canonical/blob 与上传源 {ctx.source_object_key} 均不可访问",
            )
        if stat["size"] != expected_size:
            raise DeterministicError(
                "SOURCE_SIZE_MISMATCH",
                f"输入对象大小 {stat['size']} 与登记大小 {expected_size} 不一致",
            )
        magic = self._minio.read_head_bytes(key=key, length=32)
        hinted = sniff_head(magic)
        if hinted not in (
            DetectedKind.GEOJSON,
            DetectedKind.SHAPEFILE_ZIP,
            DetectedKind.GEOPACKAGE,
        ):
            raise DeterministicError(
                "UNSUPPORTED_FORMAT",
                f"文件头 {magic[:8]!r} 不是 GeoJSON/Shapefile ZIP/GeoPackage",
            )
        ensure_source_local(minio=self._minio, engine=self._engine, ctx=ctx)
        kind = detect_file(ctx.source_path)
        if kind not in (
            DetectedKind.GEOJSON,
            DetectedKind.SHAPEFILE_ZIP,
            DetectedKind.GEOPACKAGE,
        ):
            raise DeterministicError("UNSUPPORTED_FORMAT", f"探测结果 {kind} 不是矢量格式")
        return kind

    def _step_import(self, ctx: IngestionContext, kind: DetectedKind) -> None:
        ensure_source_local(minio=self._minio, engine=self._engine, ctx=ctx)
        with session_scope(self._engine) as session:
            assets = AssetService(session)
            ext = assets.get_vector_ext(ctx.version_id)
            user_crs = ext.user_crs if ext is not None else None
            existing_count = VectorFeatureService(session).count_for_version(ctx.version_id)
            if (
                ext is not None
                and ext.feature_count is not None
                and existing_count == ext.feature_count
                and ext.footprint is not None
            ):
                version = assets.get_version_by_id(ctx.version_id)
                assert version is not None
                if version.status is AssetVersionStatus.VALIDATING:
                    assets.set_version_status(version, AssetVersionStatus.PROCESSING)
                logger.info("矢量要素已存在，跳过导入", extra={"version_id": str(ctx.version_id)})
                return

        layer = read_vector_layer(ctx.source_path, kind, user_crs=user_crs)
        features_4326, bounds = _project_layer(layer)
        min_x, min_y, max_x, max_y = bounds
        footprint = (
            f"POLYGON(({min_x!r} {min_y!r}, {max_x!r} {min_y!r}, "
            f"{max_x!r} {max_y!r}, {min_x!r} {max_y!r}, {min_x!r} {min_y!r}))"
        )
        schema = infer_property_schema([props for _, props in features_4326])
        with session_scope(self._engine) as session:
            assets = AssetService(session)
            features = VectorFeatureService(session)
            features.replace_version_features(
                ctx.version_id,
                [
                    VectorFeature(
                        id=new_uuid7(),
                        asset_version_id=ctx.version_id,
                        geometry=WKTElement(shapely_to_wkt(geom), srid=4326),
                        properties=props,
                    )
                    for geom, props in features_4326
                ],
            )
            assets.upsert_vector_ext(
                ctx.version_id,
                crs=layer.source_crs,
                geometry_type=layer.geometry_type,
                feature_count=len(features_4326),
                native_format=kind.value,
                property_schema=schema,
                footprint=WKTElement(footprint, srid=4326),
                min_x=min_x,
                min_y=min_y,
                max_x=max_x,
                max_y=max_y,
            )
            version = assets.get_version_by_id(ctx.version_id)
            assert version is not None
            if version.status is AssetVersionStatus.VALIDATING:
                assets.set_version_status(version, AssetVersionStatus.PROCESSING)

    def _step_finalize(self, ctx: IngestionContext) -> None:
        with session_scope(self._engine) as session:
            assets = AssetService(session)
            jobs = JobService(session)
            version = assets.get_version_by_id(ctx.version_id)
            assert version is not None
            ext = assets.get_vector_ext(ctx.version_id)
            count = VectorFeatureService(session).count_for_version(ctx.version_id)
            if ext is None or ext.feature_count is None or count != ext.feature_count:
                raise DeterministicError("FEATURES_MISSING", "完成前要素数与登记不一致")
            if version.status is not AssetVersionStatus.READY:
                assets.set_version_status(version, AssetVersionStatus.READY)
            job = jobs.get(ctx.job_id)
            if job is not None and job.status is not JobStatus.SUCCEEDED:
                jobs.transition(job, JobStatus.SUCCEEDED, event_type="JOB_SUCCEEDED")


def _project_layer(layer: VectorLayer) -> tuple[list[Any], tuple[float, float, float, float]]:
    assert layer.source_crs is not None
    try:
        source = CRS.from_user_input(layer.source_crs)
    except Exception as exc:
        raise DeterministicError("INVALID_CRS", f"无法解析 CRS {layer.source_crs!r}") from exc
    transformer = Transformer.from_crs(source, "EPSG:4326", always_xy=True)
    projected = []
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    for geom, props in layer.features:
        out = geom if source.to_epsg() == 4326 else shp_transform(transformer.transform, geom)
        if out.is_empty:
            continue
        minx, miny, maxx, maxy = out.bounds
        min_x, min_y = min(min_x, minx), min(min_y, miny)
        max_x, max_y = max(max_x, maxx), max(max_y, maxy)
        projected.append((out, props))
    if not projected:
        raise DeterministicError("NO_FEATURES", "投影后没有有效要素")
    return projected, (min_x, min_y, max_x, max_y)
