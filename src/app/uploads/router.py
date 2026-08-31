"""上传会话路由：HTTP 适配层。

边界：路由只做协议转换；MinIO 访问与业务事务都在 uploads.service。
"""

import logging
from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.orm import Session

from app.context import get_actor
from app.db import session_scope
from app.errors import conflict
from app.uploads.minio import MinioAdapter
from app.uploads.models import UploadSessionStatus
from app.uploads.schemas import (
    CreateSessionRequest,
    PartUrl,
    PartUrlResponse,
    SessionAbortResponse,
    SessionCompletedResponse,
    SessionCreatedResponse,
    SessionDetailResponse,
    UploadedPart,
)
from app.uploads.service import UploadService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/uploads", tags=["上传"])


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


def _get_service(
    request: Request, session: Annotated[Session, Depends(_get_session)]
) -> UploadService:
    settings = request.app.state.settings
    return UploadService(session=session, minio=MinioAdapter(settings), settings=settings)


@router.post(
    "/sessions",
    status_code=201,
    summary="创建上传会话",
    description=(
        "创建逻辑资产（或为已有资产追加版本）并返回各分片的预签名 PUT 地址。"
        "请把文件直接传到 MinIO，不要把字节发给本 API。"
    ),
    response_model=SessionCreatedResponse,
)
def create_session(
    body: CreateSessionRequest,
    request: Request,
    service: Annotated[UploadService, Depends(_get_service)],
) -> SessionCreatedResponse:
    get_actor()  # 首版匿名操作者；二期在此注入鉴权
    session, part_urls = service.create_session(
        asset_name=body.asset_name,
        asset_type=body.asset_type,
        file_name=body.file_name,
        size_bytes=body.size_bytes,
        part_count=body.part_count,
        content_type=body.content_type,
        properties=body.properties,
        source=body.source,
        asset_id=body.asset_id,
        resource_catalog_id=body.resource_catalog_id,
        satellite_id=body.satellite_id,
        sensor_id=body.sensor_id,
        acquired_at=body.acquired_at,
    )
    return SessionCreatedResponse(
        session_id=session.id,
        asset_id=session.asset_id,
        upload_id=session.minio_upload_id,
        bucket=request.app.state.settings.minio_bucket,
        object_key=session.object_key,
        part_urls=[PartUrl(part_number=p["part_number"], url=p["url"]) for p in part_urls],
        expires_in_seconds=request.app.state.settings.presign_expiry_seconds,
    )


@router.get(
    "/sessions/{session_id}",
    summary="上传会话详情",
    description="查看会话状态、已传分片和缺失分片，用于断点续传。",
    response_model=SessionDetailResponse,
)
def get_session(
    session_id: Annotated[UUID, Path(description="上传会话 ID")],
    service: Annotated[UploadService, Depends(_get_service)],
) -> SessionDetailResponse:
    session = service.get_session_required(session_id)
    parts = service.list_parts(session)
    return SessionDetailResponse.build(
        session,
        [UploadedPart(part_number=p["part_number"], size=p["size"], etag=p["etag"]) for p in parts],
    )


@router.get(
    "/sessions/{session_id}/parts/{part_number}/url",
    summary="补签分片上传地址",
    description="预签名过期后，为指定分片重新签发 PUT 地址。会话必须仍是 PENDING。",
    response_model=PartUrlResponse,
)
def get_part_url(
    session_id: Annotated[UUID, Path(description="上传会话 ID")],
    part_number: Annotated[int, Path(ge=1, description="分片序号，从 1 开始")],
    service: Annotated[UploadService, Depends(_get_service)],
) -> PartUrlResponse:
    session = service.get_session_required(session_id)
    if session.status is not UploadSessionStatus.PENDING:
        raise conflict(
            code="UPLOAD_SESSION_NOT_PENDING", detail=f"会话 {session_id} 已不在等待上传状态"
        )
    url = service.presign_part(session, part_number)
    return PartUrlResponse(
        part_number=part_number,
        url=url,
        expires_in_seconds=service.presign_expiry_seconds,
    )


@router.post(
    "/sessions/{session_id}/complete",
    summary="完成上传",
    description="合并 MinIO 分片并创建资产版本与入库任务。重复完成幂等，返回同一结果。",
    response_model=SessionCompletedResponse,
)
def complete_session(
    session_id: Annotated[UUID, Path(description="上传会话 ID")],
    service: Annotated[UploadService, Depends(_get_service)],
) -> SessionCompletedResponse:
    result = service.complete_session(session_id)
    return SessionCompletedResponse(**result)


@router.post(
    "/sessions/{session_id}/abort",
    summary="中止上传",
    description="放弃未完成的上传会话，并中止 MinIO Multipart。",
    response_model=SessionAbortResponse,
)
def abort_session(
    session_id: Annotated[UUID, Path(description="上传会话 ID")],
    service: Annotated[UploadService, Depends(_get_service)],
) -> SessionAbortResponse:
    session = service.abort_session(session_id)
    return SessionAbortResponse(session_id=str(session.id), status=session.status.value)
