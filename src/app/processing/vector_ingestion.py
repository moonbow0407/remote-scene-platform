"""矢量入库：验证 → 哈希去重 → 流式读层 → 逐条投影到 EPSG:4326 → 分批导入 → 完成。

要素写入在同一数据库事务中先删后插，失败回滚后不会留下部分 live 要素。
读取、投影、schema 统计和 bbox 均为单遍流式；按批 flush/expunge，不构建全量 ORM list。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from geoalchemy2 import WKTElement
from pyproj import CRS, Transformer
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform

from app.assets.enums import AssetStatus
from app.assets.property_schema import (
    accumulate_property_schema,
    property_schema_from_collected,
)
from app.assets.service import AssetService
from app.db import session_scope
from app.jobs.enums import JobStatus
from app.jobs.service import JobService
from app.processing.blob import bind_original, ensure_source_local, resolve_input_object
from app.processing.common import (
    IngestionContext,
    cancellation_checkpoint,
    cleanup_tmp_dir,
    preflight_tmp,
)
from app.processing.detect import DetectedKind, detect_file, sniff_head
from app.processing.errors import DeterministicError
from app.processing.vector_read import iter_vector_features, shapely_to_wkt, unify_geometry_types
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

# 1k–10k 之间：单批 ORM 对象短暂存在，flush 后立即 expunge
_FEATURE_INSERT_BATCH_SIZE = 5_000


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
            bind_original(minio=self._minio, engine=self._engine, ctx=ctx)
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
            asset = assets.get_asset_by_id(ctx.asset_id)
            assert asset is not None
            user_crs = asset.user_crs
            existing_count = VectorFeatureService(session).count_for_asset(ctx.asset_id)
            if (
                asset.feature_count is not None
                and existing_count == asset.feature_count
                and asset.footprint is not None
            ):
                if asset.status is AssetStatus.VALIDATING:
                    assets.set_status(asset, AssetStatus.PROCESSING)
                logger.info("矢量要素已存在，跳过导入", extra={"asset_id": str(ctx.asset_id)})
                return

        source_crs, features = iter_vector_features(ctx.source_path, kind, user_crs=user_crs)
        projector = _make_projector(source_crs)
        with session_scope(self._engine) as session:
            assets = AssetService(session)
            feature_svc = VectorFeatureService(session)
            feature_svc.delete_asset_features(ctx.asset_id)
            imported, bounds, schema, geometry_type = _import_projected_features(
                feature_svc,
                ctx.asset_id,
                features,
                projector,
            )
            min_x, min_y, max_x, max_y = bounds
            footprint = (
                f"POLYGON(({min_x!r} {min_y!r}, {max_x!r} {min_y!r}, "
                f"{max_x!r} {max_y!r}, {min_x!r} {max_y!r}, {min_x!r} {min_y!r}))"
            )
            assets.update_fields(
                ctx.asset_id,
                crs=source_crs,
                geometry_type=geometry_type,
                feature_count=imported,
                native_format=kind.value,
                vector_property_schema=schema,
                footprint=WKTElement(footprint, srid=4326),
                min_x=min_x,
                min_y=min_y,
                max_x=max_x,
                max_y=max_y,
            )
            version = assets.get_asset_by_id(ctx.asset_id)
            assert version is not None
            if version.status is AssetStatus.VALIDATING:
                assets.set_status(version, AssetStatus.PROCESSING)

    def _step_finalize(self, ctx: IngestionContext) -> None:
        with session_scope(self._engine) as session:
            assets = AssetService(session)
            jobs = JobService(session)
            asset = assets.get_asset_by_id(ctx.asset_id)
            assert asset is not None
            count = VectorFeatureService(session).count_for_asset(ctx.asset_id)
            if asset.feature_count is None or count != asset.feature_count:
                raise DeterministicError("FEATURES_MISSING", "完成前要素数与登记不一致")
            if asset.status is not AssetStatus.READY:
                assets.set_status(asset, AssetStatus.READY)
            job = jobs.get(ctx.job_id)
            if job is not None and job.status is not JobStatus.SUCCEEDED:
                jobs.transition(job, JobStatus.SUCCEEDED, event_type="JOB_SUCCEEDED")


def _make_projector(source_crs: str) -> Any:
    try:
        source = CRS.from_user_input(source_crs)
    except Exception as exc:
        raise DeterministicError("INVALID_CRS", f"无法解析 CRS {source_crs!r}") from exc
    if source.to_epsg() == 4326:
        return lambda geom: geom
    transformer = Transformer.from_crs(source, "EPSG:4326", always_xy=True)
    return lambda geom: shp_transform(transformer.transform, geom)


def _import_projected_features(
    feature_svc: VectorFeatureService,
    asset_id: Any,
    features: Iterator[tuple[BaseGeometry, dict[str, Any]]],
    projector: Any,
) -> tuple[int, tuple[float, float, float, float], list[dict[str, Any]], str]:
    schema_acc: dict[str, set[str]] = {}
    geom_types: set[str] = set()
    min_x = min_y = float("inf")
    max_x = max_y = float("-inf")
    seen = 0
    imported = 0
    batch: list[VectorFeature] = []
    try:
        for geom, props in features:
            seen += 1
            out = projector(geom)
            if out.is_empty:
                continue
            minx, miny, maxx, maxy = out.bounds
            min_x, min_y = min(min_x, minx), min(min_y, miny)
            max_x, max_y = max(max_x, maxx), max(max_y, maxy)
            accumulate_property_schema(schema_acc, props)
            geom_types.add(out.geom_type)
            batch.append(
                VectorFeature(
                    asset_id=asset_id,
                    geometry=WKTElement(shapely_to_wkt(out), srid=4326),
                    properties=props,
                )
            )
            imported += 1
            if len(batch) >= _FEATURE_INSERT_BATCH_SIZE:
                feature_svc.insert_feature_batch(batch)
                batch = []
        if batch:
            feature_svc.insert_feature_batch(batch)
    finally:
        close = getattr(features, "close", None)
        if close is not None:
            close()
    if imported == 0:
        if seen == 0:
            raise DeterministicError("NO_FEATURES", "矢量文件不包含可导入要素")
        raise DeterministicError("NO_FEATURES", "投影后没有有效要素")
    schema = property_schema_from_collected(schema_acc)
    return imported, (min_x, min_y, max_x, max_y), schema, unify_geometry_types(geom_types)
