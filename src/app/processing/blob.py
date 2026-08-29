"""内容寻址原文件落位：栅格/矢量/附件共用，幂等。"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.assets.enums import ArtifactKind
from app.assets.service import AssetService
from app.db import session_scope
from app.jobs.service import JobService
from app.processing.common import (
    IngestionContext,
    is_complete_local_file,
    write_chunks_atomically,
)
from app.processing.errors import DeterministicError
from app.uploads.minio import MinioAdapter

logger = logging.getLogger(__name__)


def hash_dedup_original(
    *,
    minio: MinioAdapter,
    engine: Any,
    ctx: IngestionContext,
    content_type: str,
) -> None:
    with session_scope(engine) as session:
        assets = AssetService(session)
        version = assets.get_version_by_id(ctx.version_id)
        if version is None:
            raise DeterministicError("VERSION_MISSING", f"资产版本不存在：{ctx.version_id}")
        if version.blob_id is not None:
            assert version.blob is not None
            canonical_key = version.blob.object_key
            if minio.head_object(key=canonical_key) is None:
                source_stat = minio.head_object(key=ctx.source_object_key)
                if source_stat is None:
                    raise DeterministicError(
                        "BLOB_OBJECT_MISSING",
                        f"版本已绑定 blob，但规范对象与上传源对象均不存在：{canonical_key}",
                    )
                ctx.tmp_dir.mkdir(parents=True, exist_ok=True)
                minio.download_to_file(key=ctx.source_object_key, local_path=str(ctx.source_path))
                minio.upload_file(
                    local_path=str(ctx.source_path), key=canonical_key, content_type=content_type
                )
            assets.upsert_artifact(
                version_id=ctx.version_id,
                kind=ArtifactKind.ORIGINAL,
                object_key=canonical_key,
                size_bytes=version.blob.size_bytes,
                content_type=content_type,
            )
            logger.info("版本已绑定且已验证 blob", extra={"version_id": str(ctx.version_id)})
            _record_step(engine, ctx, "hash_dedup")
            return

    digest = hashlib.sha256()
    ctx.tmp_dir.mkdir(parents=True, exist_ok=True)
    size = write_chunks_atomically(
        ctx.source_path, minio.stream_download(key=ctx.source_object_key), digest
    )
    sha = digest.hexdigest()
    canonical_key = f"original/{sha[:2]}/{sha[2:4]}/{sha}"
    if minio.head_object(key=canonical_key) is None:
        minio.upload_file(
            local_path=str(ctx.source_path), key=canonical_key, content_type=content_type
        )

    with session_scope(engine) as session:
        assets = AssetService(session)
        blob, created = assets.get_or_create_blob(
            sha256=sha, size_bytes=size, object_key=canonical_key
        )
        version = assets.get_version_by_id(ctx.version_id)
        assert version is not None
        assets.attach_blob(version, blob)
        assets.upsert_artifact(
            version_id=ctx.version_id,
            kind=ArtifactKind.ORIGINAL,
            object_key=canonical_key,
            size_bytes=size,
            content_type=content_type,
        )

    if created:
        logger.info("新内容寻址对象已落位", extra={"sha256": sha, "key": canonical_key})
    else:
        logger.info("命中内容去重，复用既有 blob", extra={"sha256": sha})
    try:
        minio.delete_object(key=ctx.source_object_key)
    except Exception:
        logger.warning("会话对象清理失败，等待后续清理", extra={"key": ctx.source_object_key})
    _record_step(engine, ctx, "hash_dedup")


def resolve_input_object(*, engine: Any, ctx: IngestionContext) -> tuple[str, int]:
    """确定本次任务应使用的输入对象及其登记大小。

    哈希去重成功后会删除 uploads/ 下的上传源对象，因此重试或 NEEDS_INPUT 恢复时
    必须改用 canonical blob，不能再固定依赖最初的 source_object_key；
    版本尚未绑定 blob（首次执行）时仍使用上传源对象。
    """
    with session_scope(engine) as session:
        version = AssetService(session).get_version_by_id(ctx.version_id)
        if version is not None and version.blob is not None:
            assert version.blob.size_bytes is not None
            return version.blob.object_key, int(version.blob.size_bytes)
    return ctx.source_object_key, ctx.source_size_bytes


def ensure_source_local(*, minio: MinioAdapter, engine: Any, ctx: IngestionContext) -> Any:
    from pathlib import Path

    key = ctx.source_object_key
    expected_size = ctx.source_size_bytes
    with session_scope(engine) as session:
        assets = AssetService(session)
        version = assets.get_version_by_id(ctx.version_id)
        assert version is not None
        if version.blob is not None:
            key = version.blob.object_key
            expected_size = version.blob.size_bytes
    if is_complete_local_file(ctx.source_path, expected_size):
        return ctx.source_path
    ctx.tmp_dir.mkdir(parents=True, exist_ok=True)
    minio.download_to_file(key=key, local_path=str(ctx.source_path))
    return Path(ctx.source_path)


def _record_step(engine: Any, ctx: IngestionContext, step: str) -> None:
    try:
        with session_scope(engine) as session:
            JobService(session).append_event(
                ctx.job_id, event_type="STEP_COMPLETED", detail={"step": step}
            )
    except Exception:
        logger.warning("步骤事件记录失败", extra={"step": step, "job_id": str(ctx.job_id)})
