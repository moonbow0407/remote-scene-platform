"""Worker 临时文件：残缺文件不得当作完整文件，目录必须可清理。"""

import hashlib
from pathlib import Path

import pytest

from app.processing.common import (
    cleanup_tmp_dir,
    is_complete_local_file,
    write_chunks_atomically,
)


def test_incomplete_local_file_is_not_treated_as_complete(tmp_path: Path) -> None:
    path = tmp_path / "source"
    path.write_bytes(b"abc")
    assert not is_complete_local_file(path, expected_size=8)
    path.write_bytes(b"abcdefgh")
    assert is_complete_local_file(path, expected_size=8)
    assert not is_complete_local_file(tmp_path / "missing", expected_size=8)


def test_write_chunks_atomically_leaves_no_partial_on_failure(tmp_path: Path) -> None:
    dest = tmp_path / "source"

    def chunks() -> object:
        yield b"abc"
        raise RuntimeError("download interrupted")

    hasher = hashlib.sha256()
    with pytest.raises(RuntimeError):
        write_chunks_atomically(dest, chunks(), hasher)
    assert not dest.exists()
    assert not dest.with_name("source.partial").exists()


def test_cleanup_tmp_dir_removes_job_workspace(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    (job_dir / "nested").mkdir(parents=True)
    (job_dir / "nested" / "source").write_bytes(b"leftover")
    cleanup_tmp_dir(job_dir)
    assert not job_dir.exists()
    cleanup_tmp_dir(job_dir)
