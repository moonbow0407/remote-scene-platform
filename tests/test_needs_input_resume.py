"""NEEDS_INPUT 恢复：没有对应 Job 时不得把版本改成 PROCESSING。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.assets.enums import AssetVersionStatus
from app.assets.service import AssetService
from app.errors import ProblemError


class _EmptyScalars:
    def first(self) -> None:
        return None


class _FakeSession:
    def __init__(self, version: object) -> None:
        self._version = version

    def scalar(self, _stmt: object) -> object:
        return self._version

    def scalars(self, _stmt: object) -> _EmptyScalars:
        return _EmptyScalars()


def test_resume_without_job_keeps_needs_input_and_raises() -> None:
    version = SimpleNamespace(id=uuid4(), status=AssetVersionStatus.NEEDS_INPUT)
    service = AssetService(_FakeSession(version))  # type: ignore[arg-type]
    service.upsert_raster_ext = lambda *args, **kwargs: None  # type: ignore[method-assign]
    service.set_version_status = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("缺少 Job 时不得改版本状态")
    )

    with pytest.raises(ProblemError) as exc_info:
        service.resume_from_needs_input(version, user_crs="EPSG:4326")  # type: ignore[arg-type]

    assert exc_info.value.code == "NEEDS_INPUT_JOB_MISSING"
    assert version.status is AssetVersionStatus.NEEDS_INPUT
