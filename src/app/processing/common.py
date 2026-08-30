"""入库流水线共用：临时目录、原子写入与任务上下文。"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from app.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class IngestionContext:
    job_id: UUID
    version_id: UUID
    source_object_key: str
    source_size_bytes: int
    tmp_dir: Path

    @property
    def source_path(self) -> Path:
        return self.tmp_dir / "source"

    @property
    def cog_path(self) -> Path:
        return self.tmp_dir / "cog.tif"

    @property
    def staged_vrt_path(self) -> Path:
        # 指派用户 CRS 的 VRT 中间层：仅元数据引用源文件，不复制像素
        return self.tmp_dir / "staged_crs.vrt"

    @property
    def thumbnail_path(self) -> Path:
        return self.tmp_dir / "thumbnail.png"

    @property
    def unpack_dir(self) -> Path:
        return self.tmp_dir / "unpack"


def write_chunks_atomically(path: Path, chunks: Any, hasher: Any) -> int:
    """流式写入临时文件后原子替换，避免半成品被当成完整文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    size = 0
    try:
        with open(tmp, "wb") as handle:
            for chunk in chunks:
                handle.write(chunk)
                hasher.update(chunk)
                size += len(chunk)
        tmp.replace(path)
        return size
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.warning("残留分片文件删除失败", extra={"path": str(tmp)})
        raise


def cleanup_tmp_dir(tmp_dir: Path) -> None:
    """删除任务临时目录；失败只记日志，不掩盖主流程错误。"""
    if not tmp_dir.exists():
        return
    try:
        shutil.rmtree(tmp_dir)
    except OSError as exc:
        logger.warning("临时目录清理失败", extra={"tmp_dir": str(tmp_dir), "detail": str(exc)})


def is_complete_local_file(path: Path, expected_size: int) -> bool:
    try:
        return path.is_file() and path.stat().st_size == expected_size
    except OSError:
        return False


def preflight_tmp(ctx: IngestionContext, settings: Settings) -> None:
    ctx.tmp_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(ctx.tmp_dir)
    required = ctx.source_size_bytes * settings.worker_tmp_min_ratio
    if usage.free < required:
        from app.processing.errors import DeterministicError

        raise DeterministicError(
            "TEMP_STORAGE_INSUFFICIENT",
            f"临时空间不足：可用 {usage.free} 字节，任务需要约 {required:.0f} 字节；"
            "请扩容 APP_WORKER_TMP_DIR 所在磁盘后重新创建版本",
        )


def cancellation_checkpoint(ctx: IngestionContext, engine: Any) -> None:
    """每个可恢复步骤前检查取消标志；取消状态与事件在同一数据库事务落库。"""
    from app.db import session_scope
    from app.jobs.service import JobService
    from app.processing.errors import ProcessingCancelledError

    with session_scope(engine) as session:
        cancelled = JobService(session).cancellation_checkpoint(ctx.job_id)
    if cancelled:
        raise ProcessingCancelledError(f"任务 {ctx.job_id} 已取消")
