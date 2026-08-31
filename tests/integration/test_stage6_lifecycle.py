"""软删除过期后物理清理：数据库行与 MinIO 对象一并移除。"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from app.assets.enums import AssetStatus, AssetType
from app.assets.lifecycle import AssetLifecycleService, ObjectCleanupService
from app.assets.models import DataAsset, ObjectCleanupTask
from app.db import make_session_factory, session_scope
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
    object_key = f"integration/stage6/{uuid4().hex}"
    source = tmp_path / "stage6-cleanup.bin"
    source.write_bytes(b"stage6-cleanup")
    minio.upload_file(local_path=str(source), key=object_key)
    assert minio.head_object(key=object_key) is not None

    try:
        with session_scope(factory) as session:
            asset = DataAsset(
                name="Stage 6 集成清理",
                asset_type=AssetType.ATTACHMENT,
                status=AssetStatus.READY,
                original_file_name=source.name,
                size_bytes=source.stat().st_size,
                original_object_key=object_key,
            )
            session.add(asset)
            session.flush()
            AssetLifecycleService(session).soft_delete(
                asset.id, retention_days=7, now=now - timedelta(days=8)
            )
            asset_id = asset.id

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
    finally:
        minio.delete_object(key=object_key)
