"""Stage 6 资产删除/恢复/引用清理决策。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker

from app.assets.enums import (
    ArtifactKind,
    AssetSource,
    AssetType,
    AssetVersionStatus,
    ObjectCleanupStatus,
)
from app.assets.lifecycle import AssetLifecycleService, ObjectCleanupService
from app.assets.models import (
    AssetArtifact,
    AssetVersion,
    DataAsset,
    ObjectBlob,
    ObjectCleanupTask,
)
from app.assets.service import AssetService
from app.db import Base, session_scope
from app.errors import ProblemError


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(_type: JSONB, compiler: object, **_kw: object) -> str:
    return "JSON"


@pytest.fixture()
def factory(monkeypatch: pytest.MonkeyPatch) -> sessionmaker[Session]:
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _foreign_keys(dbapi_conn: Any, _record: object) -> None:
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    tables = [
        Base.metadata.tables[name]
        for name in (
            "resource_catalog",
            "satellite",
            "sensor",
            "data_asset",
            "object_blob",
            "asset_version",
            "asset_artifact",
            "job",
            "job_event",
            "outbox_event",
            "object_cleanup_task",
        )
    ]
    Base.metadata.create_all(engine, tables=tables)
    monkeypatch.setattr(
        "app.monitoring.service.MonitoringService.asset_has_snapshot_references",
        lambda self, asset_id: False,
    )
    return sessionmaker(bind=engine, expire_on_commit=False)


def _seed_asset(
    session: Session, *, blob: ObjectBlob | None = None, name: str = "资产"
) -> tuple[DataAsset, AssetVersion]:
    asset = DataAsset(
        id=uuid4(), name=name, asset_type=AssetType.ATTACHMENT, source=AssetSource.UPLOAD
    )
    version = AssetVersion(
        id=uuid4(),
        asset_id=asset.id,
        version_number=1,
        status=AssetVersionStatus.READY,
        original_file_name="report.pdf",
        size_bytes=10,
        blob_id=blob.id if blob else None,
    )
    session.add_all((asset, version))
    session.flush()
    return asset, version


def test_soft_delete_hides_search_and_restore_within_window(
    factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    with session_scope(factory) as session:
        asset, _ = _seed_asset(session)
        asset_id = asset.id
        deleted = AssetLifecycleService(session).soft_delete(
            asset_id, retention_days=7, now=now
        )
        assert deleted.purge_after == now + timedelta(days=7)

    with session_scope(factory) as session:
        assert AssetService(session).get_asset(asset_id) is None
        assert AssetService(session).search_versions()[1] == 0
        restored = AssetLifecycleService(session).restore(
            asset_id, now=now + timedelta(days=6)
        )
        assert restored.deleted_at is None

    with session_scope(factory) as session:
        assert AssetService(session).get_asset(asset_id) is not None
        assert AssetService(session).search_versions()[1] == 1


def test_restore_after_retention_is_rejected(factory: sessionmaker[Session]) -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    with session_scope(factory) as session:
        asset, _ = _seed_asset(session)
        asset_id = asset.id
        AssetLifecycleService(session).soft_delete(asset_id, retention_days=7, now=now)
    with pytest.raises(ProblemError) as exc_info, session_scope(factory) as session:
        AssetLifecycleService(session).restore(asset_id, now=now + timedelta(days=7))
    assert exc_info.value.code == "ASSET_RESTORE_WINDOW_EXPIRED"


def test_shared_blob_survives_until_last_asset_is_purged(
    factory: sessionmaker[Session],
) -> None:
    now = datetime(2026, 8, 30, tzinfo=UTC)
    with session_scope(factory) as session:
        blob = ObjectBlob(
            id=uuid4(), sha256="a" * 64, object_key="original/aa/shared", size_bytes=10,
            reference_count=2,
        )
        session.add(blob)
        first, first_version = _seed_asset(session, blob=blob, name="一")
        second, second_version = _seed_asset(session, blob=blob, name="二")
        for version in (first_version, second_version):
            session.add(
                AssetArtifact(
                    id=uuid4(), asset_version_id=version.id, kind=ArtifactKind.ORIGINAL,
                    object_key=blob.object_key, size_bytes=10,
                )
            )
        first_id, second_id, blob_id = first.id, second.id, blob.id
        lifecycle = AssetLifecycleService(session)
        lifecycle.soft_delete(first_id, retention_days=7, now=now - timedelta(days=8))
        lifecycle.soft_delete(second_id, retention_days=7, now=now - timedelta(days=8))

    with session_scope(factory) as session:
        assert AssetLifecycleService(session).purge_asset(first_id, now=now)
        blob = session.get(ObjectBlob, blob_id)
        assert blob is not None and blob.reference_count == 1
        assert session.scalar(sa.select(sa.func.count()).select_from(ObjectCleanupTask)) == 0

    with session_scope(factory) as session:
        assert AssetLifecycleService(session).purge_asset(second_id, now=now)
        blob = session.get(ObjectBlob, blob_id)
        assert blob is not None and blob.reference_count == 0
        task = session.scalars(sa.select(ObjectCleanupTask)).one()
        assert task.status is ObjectCleanupStatus.PENDING

    deleted: list[str] = []
    fake_minio = type(
        "FakeMinio", (), {"delete_object": lambda self, *, key: deleted.append(key)}
    )()
    with session_scope(factory) as session:
        cleanup = ObjectCleanupService(session, fake_minio)  # type: ignore[arg-type]
        task = cleanup.claim_due(now=now, limit=10)[0]
        assert cleanup.execute(task, now=now)
    assert deleted == ["original/aa/shared"]
    with session_scope(factory) as session:
        assert session.get(ObjectBlob, blob_id) is None
