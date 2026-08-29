"""普通附件入库：校验 → 哈希去重 → 登记扩展 → READY，不触发地理处理。"""

from __future__ import annotations

import logging
from typing import Any

from app.assets.enums import ArtifactKind, AssetVersionStatus
from app.assets.service import AssetService
from app.db import session_scope
from app.jobs.enums import JobStatus
from app.jobs.service import JobService
from app.processing.blob import hash_dedup_original
from app.processing.common import IngestionContext, cleanup_tmp_dir, preflight_tmp
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
            detected, mime = self._step_validate(ctx)
            hash_dedup_original(
                minio=self._minio, engine=self._engine, ctx=ctx, content_type=mime
            )
            self._step_register(ctx, detected, mime)
            self._step_finalize(ctx)
        finally:
            cleanup_tmp_dir(ctx.tmp_dir)

    def _step_validate(self, ctx: IngestionContext) -> tuple[DetectedKind, str]:
        stat = self._minio.head_object(key=ctx.source_object_key)
        if stat is None:
            raise DeterministicError(
                "SOURCE_OBJECT_MISSING", f"源对象不存在：{ctx.source_object_key}"
            )
        if stat["size"] != ctx.source_size_bytes:
            raise DeterministicError(
                "SOURCE_SIZE_MISMATCH",
                f"源对象大小 {stat['size']} 与登记大小 {ctx.source_size_bytes} 不一致",
            )
        magic = self._minio.read_head_bytes(key=ctx.source_object_key, length=16)
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
            version = assets.get_version_by_id(ctx.version_id)
            assert version is not None
            assets.upsert_attachment_ext(
                ctx.version_id,
                mime_type=mime,
                detected_format=detected.value,
                original_file_name=version.original_file_name,
            )
            if version.status is AssetVersionStatus.VALIDATING:
                assets.set_version_status(version, AssetVersionStatus.PROCESSING)

    def _step_finalize(self, ctx: IngestionContext) -> None:
        with session_scope(self._engine) as session:
            assets = AssetService(session)
            jobs = JobService(session)
            version = assets.get_version_by_id(ctx.version_id)
            assert version is not None
            original = assets.find_artifact(ctx.version_id, ArtifactKind.ORIGINAL)
            ext = assets.get_attachment_ext(ctx.version_id)
            if original is None or ext is None:
                raise DeterministicError("ARTIFACTS_MISSING", "完成前缺少原文件工件或附件扩展")
            cog = assets.find_artifact(ctx.version_id, ArtifactKind.COG)
            if cog is not None:
                raise DeterministicError("UNEXPECTED_GEO_OUTPUT", "附件入库不得产生 COG")
            if version.status is not AssetVersionStatus.READY:
                assets.set_version_status(version, AssetVersionStatus.READY)
            job = jobs.get(ctx.job_id)
            if job is not None and job.status is not JobStatus.SUCCEEDED:
                jobs.transition(job, JobStatus.SUCCEEDED, event_type="JOB_SUCCEEDED")
