"""Worker 输入解析回归：哈希去重删除上传源后，重试/恢复必须改用 canonical blob。

回归背景：hash_dedup_original 落位 canonical blob 后会删除 uploads/ 源对象；
此前 validate 固定 head 上传源对象，导致瞬时重试与 NEEDS_INPUT 补 CRS 恢复
直接 SOURCE_OBJECT_MISSING 失败，违反 A2.5"补 CRS 后无需重新上传"。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.processing.blob import resolve_input_object
from app.processing.common import IngestionContext
from app.processing.errors import DeterministicError
from app.processing.raster_ingestion import RasterIngestion


class _FakeSession:
    """最小 Session 替身：仅支持 resolve/validate 用到的 get/add/flush/commit/close。"""

    def __init__(self, version: object) -> None:
        self._version = version

    def get(self, _cls: object, _ident: object) -> object:
        return self._version

    def add(self, _obj: object) -> None:
        return None

    def flush(self) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def _fake_factory(version: object) -> Any:
    return lambda: _FakeSession(version)


class _RecordingMinio:
    """记录 head/read 调用的 MinIO 替身；对象是否存在由 keys 决定。"""

    def __init__(self, objects: dict[str, dict[str, Any]]) -> None:
        self._objects = objects
        self.headed: list[str] = []

    def head_object(self, *, key: str) -> dict[str, Any] | None:
        self.headed.append(key)
        stat = self._objects.get(key)
        return {"size": stat["size"], "etag": "x"} if stat is not None else None

    def read_head_bytes(self, *, key: str, length: int) -> bytes:
        assert key in self._objects, f"读取头字节的对象应存在：{key}"
        return b"II*\x00"


class _ForbiddenMinio:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"测试不应访问 MinIO 方法：{name}")


def _make_ctx(tmp_path: Path, source_key: str = "uploads/s1/src.tif") -> IngestionContext:
    return IngestionContext(
        job_id=uuid4(),
        version_id=uuid4(),
        source_object_key=source_key,
        source_size_bytes=4,
        tmp_dir=tmp_path / "job",
    )


def _blob_version(key: str, size: int) -> Any:
    return SimpleNamespace(blob=SimpleNamespace(object_key=key, size_bytes=size))


def _no_blob_version() -> Any:
    return SimpleNamespace(blob=None)


def test_resolve_input_prefers_canonical_blob(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    version = _blob_version("original/ab/cd/" + "a" * 64, 4)
    key, size = resolve_input_object(engine=_fake_factory(version), ctx=ctx)
    assert key == "original/ab/cd/" + "a" * 64
    assert size == 4


def test_resolve_input_falls_back_to_upload_source(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    key, size = resolve_input_object(engine=_fake_factory(_no_blob_version()), ctx=ctx)
    assert key == "uploads/s1/src.tif"
    assert size == 4


def test_raster_validate_uses_canonical_blob_after_source_deleted(tmp_path: Path) -> None:
    """首次执行已完成哈希去重（源对象已删）后瞬时重试：validate 必须基于 canonical 对象通过。"""
    ctx = _make_ctx(tmp_path)
    canonical = "original/ab/cd/" + "a" * 64
    version = _blob_version(canonical, 4)
    minio = _RecordingMinio({canonical: {"size": 4}})
    ingestion = RasterIngestion(
        settings=object(),
        minio=minio,
        engine=_fake_factory(version),  # type: ignore[arg-type]
    )
    ingestion._step_validate(ctx)
    assert "uploads/s1/src.tif" not in minio.headed


def test_raster_validate_falls_back_to_source_when_canonical_missing(tmp_path: Path) -> None:
    """canonical 对象缺失但上传源仍在（此前删除失败）：回退校验源对象，交给 hash_dedup 修复。"""
    ctx = _make_ctx(tmp_path)
    canonical = "original/ab/cd/" + "a" * 64
    version = _blob_version(canonical, 4)
    minio = _RecordingMinio({"uploads/s1/src.tif": {"size": 4}})
    ingestion = RasterIngestion(
        settings=object(),
        minio=minio,
        engine=_fake_factory(version),  # type: ignore[arg-type]
    )
    ingestion._step_validate(ctx)
    assert minio.headed == [canonical, "uploads/s1/src.tif"]


def test_raster_validate_fails_when_both_objects_missing(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    canonical = "original/ab/cd/" + "a" * 64
    version = _blob_version(canonical, 4)
    ingestion = RasterIngestion(
        settings=object(),
        minio=_RecordingMinio({}),
        engine=_fake_factory(version),  # type: ignore[arg-type]
    )
    with pytest.raises(DeterministicError) as exc_info:
        ingestion._step_validate(ctx)
    assert exc_info.value.code == "SOURCE_OBJECT_MISSING"
