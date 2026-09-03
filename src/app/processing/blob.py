"""原件落位：上传对象即原件，不做内容去重。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.db import session_scope
from app.imagery.service import ImageryService
from app.jobs.service import JobService
from app.processing.common import (
    IngestionContext,
    is_complete_local_file,
)
from app.processing.errors import DeterministicError
from app.uploads.minio import MinioAdapter

logger = logging.getLogger(__name__)


def bind_original(*, minio: MinioAdapter, engine: Any, ctx: IngestionContext) -> None:
    """确认上传对象存在，并写入 original_object_key。"""
    with session_scope(engine) as session:
        imagery = ImageryService(session)
        record = imagery.get_by_id(ctx.owner_kind, ctx.owner_id)
        if record is None:
            raise DeterministicError(
                "RECORD_MISSING", f"记录不存在：{ctx.owner_kind} {ctx.owner_id}"
            )
        key = record.original_object_key or ctx.source_object_key
        if minio.head_object(key=key) is None:
            raise DeterministicError("SOURCE_OBJECT_MISSING", f"原件对象不存在：{key}")
        if record.original_object_key != key:
            imagery.update_fields(ctx.owner_kind, ctx.owner_id, original_object_key=key)
    _record_step(engine, ctx, "bind_original")


def resolve_input_object(*, engine: Any, ctx: IngestionContext) -> tuple[str, int]:
    with session_scope(engine) as session:
        record = ImageryService(session).get_by_id(ctx.owner_kind, ctx.owner_id)
        if record is not None and record.original_object_key:
            return record.original_object_key, int(record.size_bytes)
    return ctx.source_object_key, ctx.source_size_bytes


def ensure_source_local(*, minio: MinioAdapter, engine: Any, ctx: IngestionContext) -> Path:
    key, expected_size = resolve_input_object(engine=engine, ctx=ctx)
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
