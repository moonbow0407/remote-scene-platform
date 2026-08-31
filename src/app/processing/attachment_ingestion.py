"""普通附件入库：校验 → 绑定原件 → 登记扩展 → READY，不触发地理处理。"""

from __future__ import annotations

import logging
from typing import Any

from app.assets.enums import AssetStatus
from app.assets.service import AssetService
from app.db import session_scope
from app.jobs.enums import JobStatus
from app.jobs.service import JobService
from app.processing.blob import bind_original, resolve_input_object
from app.processing.common import (
    IngestionContext,
    cancellation_checkpoint,
    cleanup_tmp_dir,
    preflight_tmp,
)
from app.processing.detect import DetectedKind, sniff_head
from app.processing.errors import DeterministicError
from app.settings import Settings
from app.uploads.minio import MinioAdapter

logger = logging.getLogger(__name__)


class AttachmentIngestion:
    def __init__(self, *, settings: Settings, minio: MinioAdapter, engine: Any) -> None:
        self._settings = settings
        self._minio = minio
        self._engine = engine

    def run(self, ctx: IngestionContext) -> None:
        cleanup_tmp_dir(ctx.tmp_dir)
        try:
            preflight_tmp(ctx, self._settings)
            cancellation_checkpoint(ctx, self._engine)
            detected, mime = self._step_validate(ctx)
            cancellation_checkpoint(ctx, self._engine)
            bind_original(minio=self._minio, engine=self._engine, ctx=ctx)
            cancellation_checkpoint(ctx, self._engine)
            self._step_register(ctx, detected, mime)
            cancellation_checkpoint(ctx, self._engine)
            self._step_finalize(ctx)
        finally:
            cleanup_tmp_dir(ctx.tmp_dir)

    def _step_validate(self, ctx: IngestionContext) -> tuple[DetectedKind, str]:
        key, expected_size = resolve_input_object(engine=self._engine, ctx=ctx)
        stat = self._minio.head_object(key=key)
        if stat is None and key != ctx.source_object_key:
            key = ctx.source_object_key
            expected_size = ctx.source_size_bytes
            stat = self._minio.head_object(key=key)
        if stat is None:
            raise DeterministicError(
                "SOURCE_OBJECT_MISSING",
                f"输入对象不存在：{ctx.source_object_key}",
            )
        if stat["size"] != expected_size:
            raise DeterministicError(
                "SOURCE_SIZE_MISMATCH",
                f"输入对象大小 {stat['size']} 与登记大小 {expected_size} 不一致",
            )
        magic = self._minio.read_head_bytes(key=key, length=16)
        kind = sniff_head(magic)
        mime = {
            DetectedKind.PDF: "application/pdf",
            DetectedKind.TIFF: "image/tiff",
            DetectedKind.GEOJSON: "application/geo+json",
        }.get(kind, "application/octet-stream")
        return kind, mime

    def _step_register(self, ctx: IngestionContext, detected: DetectedKind, mime: str) -> None:
        with session_scope(self._engine) as session:
            assets = AssetService(session)
            asset = assets.get_asset_by_id(ctx.asset_id)
            assert asset is not None
            assets.update_fields(
                ctx.asset_id,
                mime_type=mime,
                detected_format=detected.value,
            )
            if asset.status is AssetStatus.VALIDATING:
                assets.set_status(asset, AssetStatus.PROCESSING)

    def _step_finalize(self, ctx: IngestionContext) -> None:
        with session_scope(self._engine) as session:
            assets = AssetService(session)
            jobs = JobService(session)
            asset = assets.get_asset_by_id(ctx.asset_id)
            assert asset is not None
            if asset.original_object_key is None or asset.mime_type is None:
                raise DeterministicError("ARTIFACTS_MISSING", "完成前缺少原件或附件元数据")
            if asset.cog_object_key is not None:
                raise DeterministicError("UNEXPECTED_GEO_OUTPUT", "附件入库不得产生 COG")
            if asset.status is not AssetStatus.READY:
                assets.set_status(asset, AssetStatus.READY)
            job = jobs.get(ctx.job_id)
            if job is not None and job.status is not JobStatus.SUCCEEDED:
                jobs.transition(job, JobStatus.SUCCEEDED, event_type="JOB_SUCCEEDED")
