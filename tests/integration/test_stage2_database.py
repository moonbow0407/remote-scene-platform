"""Stage 2 PostgreSQL 事务、认领和去重并发接缝。"""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import cast
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.assets.enums import AssetSource, AssetType, AssetVersionStatus
from app.assets.models import AssetVersion, ObjectBlob
from app.assets.service import AssetService
from app.db import make_session_factory, session_scope
from app.ids import new_uuid7
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
    session_factory = make_session_factory(engine)
    yield session_factory
    engine.dispose()


class _ExistingObjectMinio:
    """模拟已在 MinIO 合并成功、数据库事务尚未提交的恢复场景。"""

    def list_parts(self, *, key: str, upload_id: str) -> list[dict[str, object]]:
        return []

    def head_object(self, *, key: str) -> dict[str, object]:
        return {"size": 8, "etag": "fixture"}


def test_concurrent_upload_completion_creates_one_version_and_job(
    factory: sessionmaker[Session],
) -> None:
    session_id = new_uuid7()
    with session_scope(factory) as session:
        asset = AssetService(session).create_asset(
            name="并发完成测试",
            asset_type=AssetType.RASTER,
            source=AssetSource.UPLOAD,
        )
        asset_id = asset.id
        session.add(
            UploadSession(
                id=session_id,
                asset_id=asset_id,
                status=UploadSessionStatus.PENDING,
                minio_upload_id="already-completed",
                object_key=f"uploads/{session_id}/fixture.tif",
                file_name="fixture.tif",
                size_bytes=8,
                part_count=1,
                content_type="image/tiff",
            )
        )

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
                sa.select(sa.func.count())
                .select_from(AssetVersion)
                .where(AssetVersion.asset_id == asset_id)
            )
            == 1
        )
        assert session.scalar(sa.select(sa.func.count()).select_from(Job)) == 1
        assert session.scalar(sa.select(sa.func.count()).select_from(OutboxEvent)) == 1


def test_pending_job_is_claimed_once_under_duplicate_delivery(
    factory: sessionmaker[Session],
) -> None:
    with session_scope(factory) as session:
        assets = AssetService(session)
        asset = assets.create_asset(
            name="重复投递测试",
            asset_type=AssetType.RASTER,
            source=AssetSource.UPLOAD,
        )
        version = assets.create_version(
            asset_id=asset.id,
            original_file_name="fixture.tif",
            size_bytes=8,
            status=AssetVersionStatus.VALIDATING,
        )
        job, _ = JobService(session).create_job_with_outbox(
            job_type=JobType.RASTER_INGESTION,
            asset_version_id=version.id,
            payload={"asset_version_id": str(version.id)},
        )
        job_id = job.id

    def claim() -> JobStatus:
        with session_scope(factory) as session:
            return JobService(session).claim_for_run(job_id).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = list(executor.map(lambda _: claim(), range(2)))

    assert statuses == [JobStatus.RUNNING, JobStatus.RUNNING]
    with session_scope(factory) as session:
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status is JobStatus.RUNNING
        assert job.attempt == 1
        assert (
            session.scalar(
                sa.select(sa.func.count())
                .select_from(JobEvent)
                .where(
                    JobEvent.job_id == job_id,
                    JobEvent.event_type == "JOB_CLAIMED",
                )
            )
            == 1
        )


def test_concurrent_blob_creation_reuses_one_row(factory: sessionmaker[Session]) -> None:
    sha256 = uuid4().hex * 2
    object_key = f"original/{sha256[:2]}/{sha256[2:4]}/{sha256}"

    def create_reference() -> tuple[str, bool]:
        with session_scope(factory) as session:
            blob, created = AssetService(session).get_or_create_blob(
                sha256=sha256,
                size_bytes=8,
                object_key=object_key,
            )
            return str(blob.id), created

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create_reference(), range(2)))

    assert results[0][0] == results[1][0]
    assert sorted(created for _, created in results) == [False, True]
    with session_scope(factory) as session:
        blob = session.scalar(sa.select(ObjectBlob).where(ObjectBlob.sha256 == sha256))
        assert blob is not None
        assert blob.reference_count == 2
