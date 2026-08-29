"""上传会话回归：业务校验先于 MinIO Multipart 创建；会话保存本批 properties。

回归背景：
- 无效请求（如资源目录不存在）先创建 Multipart 再校验，会遗留孤儿分片上传；
- 给已有资产追加版本时版本元数据从 DataAsset.properties 抄一份，
  本批请求声明的 properties/acquired_at 被丢弃。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.assets.enums import AssetSource, AssetType, AssetVersionStatus
from app.assets.models import AssetVersion, DataAsset
from app.assets.service import AssetService
from app.db import Base, session_scope
from app.errors import ProblemError
from app.settings import Settings
from app.uploads.models import UploadSession, UploadSessionStatus
from app.uploads.service import UploadService

_TABLES = (
    "data_asset",
    "asset_version",
    "upload_session",
    "job",
    "job_event",
    "outbox_event",
    "property_schema",
    "resource_catalog",
    "satellite",
    "sensor",
)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(_type: JSONB, compiler: object, **_kw: object) -> str:
    return "JSON"


@pytest.fixture()
def factory() -> Iterator[sessionmaker[Session]]:
    engine = sa.create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in _TABLES])
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


class _RecordingMinio:
    """记录 Multipart 调用的 MinIO 替身；合并后的对象大小为 8 字节。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_multipart_upload(self, *, key: str, content_type: str | None) -> str:
        self.calls.append(("create", key))
        return "upload-1"

    def abort_multipart_upload(self, *, key: str, upload_id: str) -> None:
        self.calls.append(("abort", key))

    def presign_part_url(
        self, *, key: str, upload_id: str, part_number: int, expires_in: int
    ) -> str:
        return f"https://minio/{key}?part={part_number}"

    def list_parts(self, *, key: str, upload_id: str) -> list[dict[str, Any]]:
        return [{"part_number": 1, "size": 8, "etag": "e"}]

    def head_object(self, *, key: str) -> dict[str, Any] | None:
        return {"size": 8, "etag": "x"}

    def complete_multipart_upload(
        self, *, key: str, upload_id: str, parts: list[dict[str, Any]]
    ) -> None:
        self.calls.append(("complete", key))


def _create_asset(factory: sessionmaker[Session]) -> UUID:
    with session_scope(factory) as session:
        asset = AssetService(session).create_asset(
            name="追加资产", asset_type=AssetType.RASTER, source=AssetSource.UPLOAD
        )
        return asset.id


def test_create_session_skips_multipart_when_validation_fails(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """资源目录不存在（404）等无效请求不得创建 MinIO Multipart。"""
    minio = _RecordingMinio()
    # 阻断 AssetService 之外对 MinIO 的调用次序观察：无需 patch，直接断言调用序列
    with pytest.raises(ProblemError) as exc_info:
        session = factory()
        try:
            UploadService(session=session, minio=minio, settings=Settings()).create_session(  # type: ignore[arg-type]
                asset_name="资产",
                asset_type=AssetType.RASTER,
                file_name="a.tif",
                size_bytes=8,
                part_count=1,
                content_type=None,
                properties={},
                source=AssetSource.UPLOAD,
                resource_catalog_id=uuid4(),
            )
        finally:
            session.close()
    assert exc_info.value.status == 404
    assert minio.calls == []


def test_create_session_persists_batch_properties(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    minio = _RecordingMinio()
    asset_id = _create_asset(factory)
    properties = {"acquired_at": "2026-01-01T08:00:00+08:00", "mission": "M-1"}

    session_obj = factory()
    try:
        upload_service = UploadService(session=session_obj, minio=minio, settings=Settings())  # type: ignore[arg-type]
        created, _urls = upload_service.create_session(
            asset_name="追加资产",
            asset_type=AssetType.RASTER,
            file_name="b.tif",
            size_bytes=8,
            part_count=1,
            content_type=None,
            properties=properties,
            source=AssetSource.UPLOAD,
            asset_id=asset_id,
        )
        assert minio.calls[0][0] == "create"
        stored = session_obj.get(UploadSession, created.id)
        assert stored is not None
        assert dict(stored.properties) == properties
    finally:
        session_obj.rollback()
        session_obj.close()


def test_complete_session_uses_session_properties_for_new_version(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """追加版本：AssetVersion 的元数据取本批会话声明，不从 DataAsset.properties 抄。"""
    minio = _RecordingMinio()
    asset_id = _create_asset(factory)
    session_id = uuid4()
    batch_properties = {"acquired_at": "2026-01-01T08:00:00+08:00", "mission": "M-2"}

    with session_scope(factory) as session:
        session.add(
            UploadSession(
                id=session_id,
                asset_id=asset_id,
                status=UploadSessionStatus.PENDING,
                minio_upload_id="upload-1",
                object_key=f"uploads/{session_id}/b.tif",
                file_name="b.tif",
                size_bytes=8,
                part_count=1,
                content_type=None,
                properties=batch_properties,
            )
        )
        # 旧版本带旧的 acquired_at，验证不会被抄到新版本
        session.add(
            AssetVersion(
                id=uuid4(),
                asset_id=asset_id,
                version_number=1,
                status=AssetVersionStatus.READY,
                original_file_name="a.tif",
                size_bytes=8,
                properties={"acquired_at": "2025-01-01T00:00:00+00:00"},
            )
        )

    session_obj = factory()
    try:
        upload_service = UploadService(session=session_obj, minio=minio, settings=Settings())  # type: ignore[arg-type]
        result = upload_service.complete_session(session_id)
        version = session_obj.get(AssetVersion, UUID(str(result["asset_version_id"])))
        assert version is not None
        assert version.version_number == 2
        assert dict(version.properties) == batch_properties
        # sqlite 读回 naive datetime（PostgreSQL 为 timestamptz）；值应为 UTC 墙钟时间
        assert version.acquired_at is not None
        assert version.acquired_at.replace(tzinfo=UTC) == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert (
            dict(
                session_obj.get(DataAsset, asset_id).properties  # type: ignore[union-attr]
            )
            == {}
        )
    finally:
        session_obj.rollback()
        session_obj.close()


def test_create_session_appends_to_existing_asset_and_multipart_created_after_checks(
    factory: sessionmaker[Session],
) -> None:
    """合法请求：先完成资产/目录校验（含资产类型匹配），再创建 Multipart。"""
    minio = _RecordingMinio()
    asset_id = _create_asset(factory)
    session_obj = factory()
    try:
        upload_service = UploadService(session=session_obj, minio=minio, settings=Settings())  # type: ignore[arg-type]
        _created, _urls = upload_service.create_session(
            asset_name="追加资产",
            asset_type=AssetType.RASTER,
            file_name="b.tif",
            size_bytes=8,
            part_count=2,
            content_type=None,
            properties={},
            source=AssetSource.UPLOAD,
            asset_id=asset_id,
        )
    finally:
        session_obj.rollback()
        session_obj.close()
    assert [name for name, _key in minio.calls] == ["create"]
