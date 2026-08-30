"""Stage 6 PostgreSQL + MinIO 生命周期清理接缝。

需要显式提供已执行 ``alembic upgrade head`` 的 PostgreSQL/PostGIS 数据库，以及
可写的 MinIO 测试桶。测试只创建并删除带随机键的单个小对象，不复用业务对象。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.assets.enums import AssetSource, AssetType, AssetVersionStatus
from app.assets.lifecycle import AssetLifecycleService, ObjectCleanupService
from app.assets.models import AssetVersion, DataAsset, ObjectBlob, ObjectCleanupTask
from app.db import make_session_factory, session_scope
from app.ids import new_uuid7
from app.settings import Settings
from app.uploads.minio import MinioAdapter

DATABASE_URL = os.getenv("APP_INTEGRATION_DATABASE_URL")
MINIO_ENDPOINT = os.getenv("APP_INTEGRATION_MINIO_ENDPOINT")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DATABASE_URL is None, reason="未提供 APP_INTEGRATION_DATABASE_URL"),
    pytest.mark.skipif(MINIO_ENDPOINT is None, reason="未提供 APP_INTEGRATION_MINIO_ENDPOINT"),
]


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    assert DATABASE_URL is not None
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def minio() -> MinioAdapter:
    assert MINIO_ENDPOINT is not None
    return MinioAdapter(
        Settings(
            minio_endpoint=MINIO_ENDPOINT,
            minio_public_endpoint=MINIO_ENDPOINT,
            minio_access_key=os.getenv("APP_INTEGRATION_MINIO_ACCESS_KEY", "minioadmin"),
            minio_secret_key=os.getenv("APP_INTEGRATION_MINIO_SECRET_KEY", "minioadmin"),
            minio_bucket=os.getenv("APP_INTEGRATION_MINIO_BUCKET", "remote-scene"),
        )
    )


def test_expired_asset_removes_database_rows_and_minio_object(
    factory: sessionmaker[Session], minio: MinioAdapter, tmp_path: Path
) -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    sha256 = uuid4().hex * 2
    object_key = f"integration/stage6/{sha256}"
    source = tmp_path / "stage6-cleanup.bin"
    source.write_bytes(b"stage6-cleanup")
    minio.upload_file(local_path=str(source), key=object_key)
    assert minio.head_object(key=object_key) is not None

    asset_id = new_uuid7()
    blob_id = new_uuid7()
    try:
        with session_scope(factory) as session:
            session.add(
                ObjectBlob(
                    id=blob_id,
                    sha256=sha256,
                    object_key=object_key,
                    size_bytes=source.stat().st_size,
                    reference_count=1,
                )
            )
            session.add(
                DataAsset(
                    id=asset_id,
                    name="Stage 6 集成清理",
                    asset_type=AssetType.ATTACHMENT,
                    source=AssetSource.UPLOAD,
                )
            )
            session.add(
                AssetVersion(
                    id=new_uuid7(),
                    asset_id=asset_id,
                    version_number=1,
                    status=AssetVersionStatus.READY,
                    original_file_name=source.name,
                    size_bytes=source.stat().st_size,
                    blob_id=blob_id,
                )
            )
            AssetLifecycleService(session).soft_delete(
                asset_id, retention_days=7, now=now - timedelta(days=8)
            )

        with session_scope(factory) as session:
            assert AssetLifecycleService(session).purge_asset(asset_id, now=now)
            task = session.scalar(
                sa.select(ObjectCleanupTask).where(ObjectCleanupTask.object_key == object_key)
            )
            assert task is not None

        with session_scope(factory) as session:
            cleanup = ObjectCleanupService(session, minio)
            task = session.scalar(
                sa.select(ObjectCleanupTask)
                .where(ObjectCleanupTask.object_key == object_key)
                .with_for_update()
            )
            assert task is not None
            assert cleanup.execute(task, now=now)

        assert minio.head_object(key=object_key) is None
        with session_scope(factory) as session:
            assert session.get(DataAsset, asset_id) is None
            assert session.get(ObjectBlob, blob_id) is None
    finally:
        # S3 DeleteObject 幂等；失败路径也只清理本测试生成的随机键。
        minio.delete_object(key=object_key)
