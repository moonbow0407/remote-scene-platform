"""入库并发接缝：上传完成幂等、Job 认领一次、完成/中止竞争。

需要显式提供 `APP_INTEGRATION_DATABASE_URL`（已 `alembic upgrade head`
的 PostgreSQL/PostGIS）；未提供时跳过。
"""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import cast
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.assets.enums import AssetType
from app.assets.service import AssetService
from app.db import make_session_factory, session_scope
from app.jobs.enums import JobStatus, JobType
from app.jobs.models import Job, JobEvent, OutboxEvent
from app.jobs.service import JobService
from app.settings import Settings
from app.uploads.minio import MinioAdapter
from app.uploads.models import UploadSession, UploadSessionStatus
from app.uploads.service import UploadService

DATABASE_URL = os.getenv("APP_INTEGRATION_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DATABASE_URL is None, reason="未提供 APP_INTEGRATION_DATABASE_URL"),
]


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    assert DATABASE_URL is not None
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True)
    yield make_session_factory(engine)
    engine.dispose()


class _ExistingObjectMinio:
    def list_parts(self, *, key: str, upload_id: str) -> list[dict[str, object]]:
        return []

    def head_object(self, *, key: str) -> dict[str, object]:
        return {"size": 8, "etag": "fixture"}


def _pending_session(session: Session, *, asset_id: int, upload_id: str) -> int:
    row = UploadSession(
        asset_id=asset_id,
        status=UploadSessionStatus.PENDING,
        minio_upload_id=upload_id,
        object_key=f"uploads/{uuid4()}/fixture.tif",
        file_name="fixture.tif",
        size_bytes=8,
        part_count=1,
        content_type="image/tiff",
    )
    session.add(row)
    session.flush()
    return row.id


def test_concurrent_upload_completion_creates_one_job(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        asset = AssetService(session).create_asset(
            name="fixture.tif",
            asset_type=AssetType.RASTER,
            original_file_name="fixture.tif",
            size_bytes=8,
        )
        asset_id = asset.id
        session_id = _pending_session(session, asset_id=asset_id, upload_id="already-completed")

    minio = cast(MinioAdapter, _ExistingObjectMinio())
    settings = Settings()

    def complete() -> dict[str, object]:
        with session_scope(factory) as session:
            return UploadService(session, minio, settings).complete_session(session_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: complete(), range(2)))

    assert results[0] == results[1]
    with session_scope(factory) as session:
        assert (
            session.scalar(
                sa.select(sa.func.count()).select_from(Job).where(Job.asset_id == asset_id)
            )
            == 1
        )
        job_ids = sa.select(Job.id).where(Job.asset_id == asset_id)
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.aggregate_id.in_(job_ids))
            )
            == 1
        )


def test_pending_job_is_claimed_once_under_duplicate_delivery(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        assets = AssetService(session)
        asset = assets.create_asset(
            name="fixture.tif",
            asset_type=AssetType.RASTER,
            original_file_name="fixture.tif",
            size_bytes=8,
        )
        job, _ = JobService(session).create_job_with_outbox(
            job_type=JobType.RASTER_INGESTION,
            asset_id=asset.id,
            payload={"asset_id": str(asset.id)},
        )
        job_id = job.id

    def claim() -> tuple[JobStatus, bool]:
        with session_scope(factory) as session:
            result = JobService(session).claim_for_run(job_id)
            return result.job.status, result.acquired

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: claim(), range(2)))

    assert sorted(acquired for _, acquired in outcomes) == [False, True]
    assert all(status is JobStatus.RUNNING for status, _ in outcomes)
    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.RUNNING
        assert job.attempt == 1
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(JobEvent)
                .where(JobEvent.job_id == job_id, JobEvent.event_type == "JOB_CLAIMED")
            )
            == 1
        )


class _CompleteAbortMinio:
    def list_parts(self, *, key: str, upload_id: str) -> list[dict[str, object]]:
        return []

    def head_object(self, *, key: str) -> dict[str, object]:
        return {"size": 8, "etag": "fixture"}

    def abort_multipart_upload(self, *, key: str, upload_id: str) -> None:
        return None


def test_concurrent_complete_and_abort_do_not_diverge(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        asset = AssetService(session).create_asset(
            name="fixture.tif",
            asset_type=AssetType.RASTER,
            original_file_name="fixture.tif",
            size_bytes=8,
        )
        asset_id = asset.id
        session_id = _pending_session(session, asset_id=asset_id, upload_id="upload-id")

    minio = cast(MinioAdapter, _CompleteAbortMinio())
    settings = Settings()

    def complete() -> str:
        try:
            with session_scope(factory) as session:
                UploadService(session, minio, settings).complete_session(session_id)
            return "completed"
        except Exception as exc:
            return f"complete_error:{type(exc).__name__}"

    def abort() -> str:
        try:
            with session_scope(factory) as session:
                UploadService(session, minio, settings).abort_session(session_id)
            return "aborted"
        except Exception as exc:
            return f"abort_error:{type(exc).__name__}"

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(complete)
        second = executor.submit(abort)
        results = {first.result(), second.result()}

    with session_scope(factory) as session:
        row = session.get(UploadSession, session_id)
        assert row is not None
        job_count = int(
            session.scalar(
                sa.select(sa.func.count()).select_from(Job).where(Job.asset_id == asset_id)
            )
            or 0
        )
        if row.status is UploadSessionStatus.COMPLETED:
            assert job_count == 1
            assert any(item.startswith("abort_error:") for item in results)
        else:
            assert row.status is UploadSessionStatus.ABORTED
            assert job_count == 0
            assert any(item.startswith("complete_error:") for item in results)
