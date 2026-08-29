"""Multipart 完成与 API 时间不变量的纯逻辑回归测试。"""

from datetime import UTC
from typing import Any

import pytest

from app.errors import ProblemError
from app.uploads.service import _parse_acquired_at, _validate_uploaded_parts


def test_complete_parts_require_exact_numbers_and_size() -> None:
    _validate_uploaded_parts(
        [
            {"part_number": 1, "size": 5, "etag": "a"},
            {"part_number": 2, "size": 3, "etag": "b"},
        ],
        expected_count=2,
        expected_size=8,
    )


@pytest.mark.parametrize(
    ("parts", "expected_count", "expected_size", "code"),
    [
        ([{"part_number": 1, "size": 5}], 2, 5, "UPLOAD_PARTS_INCOMPLETE"),
        ([{"part_number": 1, "size": 5}], 1, 6, "UPLOAD_SIZE_MISMATCH"),
    ],
)
def test_incomplete_or_wrong_size_parts_rejected(
    parts: list[dict[str, Any]], expected_count: int, expected_size: int, code: str
) -> None:
    with pytest.raises(ProblemError) as exc_info:
        _validate_uploaded_parts(parts, expected_count=expected_count, expected_size=expected_size)
    assert exc_info.value.code == code


def test_acquired_at_requires_timezone_and_normalizes_utc() -> None:
    parsed = _parse_acquired_at({"acquired_at": "2026-08-29T16:00:00+08:00"})
    assert parsed is not None
    assert parsed.tzinfo is UTC
    assert parsed.hour == 8

    with pytest.raises(ProblemError):
        _parse_acquired_at({"acquired_at": "2026-08-29T16:00:00"})
