"""上传会话路由：HTTP 适配层。"""

import logging
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.orm import Session

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
    summary="创建上传",
    description=(
        "提交文件名、大小、kind 和 data_source_id。返回分片的临时 PUT 地址和记录编号。"
        "把每一片直接 PUT 到对应地址，不要把文件 POST 到本接口。"
        "全部传完后调用「完成上传」。"
    ),
    response_model=SessionCreatedResponse,
)
def create_session(
    body: CreateSessionRequest,
    request: Request,
    service: Annotated[UploadService, Depends(_get_service)],
) -> SessionCreatedResponse:
    session, part_urls = service.create_session(
        file_name=body.file_name,
        size_bytes=body.size_bytes,
        content_type=body.content_type,
        kind=body.kind,
        data_source_id=body.data_source_id,
    )
    return SessionCreatedResponse(
        session_id=session.id,
        kind=session.owner_kind,
        record_id=session.owner_id,
        part_urls=[PartUrl(part_number=p["part_number"], url=p["url"]) for p in part_urls],
        expires_in_seconds=request.app.state.settings.presign_expiry_seconds,
    )


@router.get(
    "/sessions/{session_id}",
    summary="上传详情",
    description="查看已传到哪、还缺哪几片。中断后续传时用。",
    response_model=SessionDetailResponse,
)
def get_session(
    session_id: Annotated[int, Path(description="本次上传编号")],
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
    summary="补签分片地址",
    description="某一片的临时地址过期后，用本接口重新拿一个 PUT 地址。",
    response_model=PartUrlResponse,
)
def get_part_url(
    session_id: Annotated[int, Path(description="本次上传编号")],
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
    description=(
        "合并已上传的分片并开始后台处理。"
        "之后用返回的 kind 和 record_id 请求详情，直到 READY、FAILED 或 NEEDS_INPUT。"
        "重复调用会返回同一结果，不会再传一遍。"
    ),
    response_model=SessionCompletedResponse,
)
def complete_session(
    session_id: Annotated[int, Path(description="本次上传编号")],
    service: Annotated[UploadService, Depends(_get_service)],
) -> SessionCompletedResponse:
    result = service.complete_session(session_id)
    return SessionCompletedResponse(**result)


@router.post(
    "/sessions/{session_id}/abort",
    summary="中止上传",
    description="取消这次上传。对应记录会记为失败。已经完成的上传不能中止。",
    response_model=SessionAbortResponse,
)
def abort_session(
    session_id: Annotated[int, Path(description="本次上传编号")],
    service: Annotated[UploadService, Depends(_get_service)],
) -> SessionAbortResponse:
    session = service.abort_session(session_id)
    return SessionAbortResponse(session_id=session.id, status=session.status.value)
