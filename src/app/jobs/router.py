"""Job 路由：进度轮询（首版不提供 SSE，事件模型为二期预留边界）。"""

import logging
from collections.abc import Iterator
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Request
from sqlalchemy.orm import Session

from app.assets.service import AssetService
from app.db import session_scope
from app.jobs.enums import JobStatus
from app.jobs.models import JobEvent
from app.jobs.schemas import CancelJobResponse, JobEventItem, JobResponse
from app.jobs.service import JobService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["任务"])


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


@router.get(
    "/{job_id}",
    summary="查询任务",
    description=(
        "轮询入库或监测任务进度。首版不提供 SSE，请按此接口轮询。"
        "管理页请轮询 GET /assets/{id}。本接口留给运维。"
    ),
    response_model=JobResponse,
)
def get_job(
    job_id: Annotated[int, Path(description="任务 ID")],
    request: Request,
    session: Annotated[Session, Depends(_get_session)],
) -> JobResponse:
    service = JobService(session)
    job = service.get_required(job_id)
    events = session.scalars(
        sa.select(JobEvent)
        .where(JobEvent.job_id == job_id)
        .order_by(JobEvent.created_at)
        .limit(200)
    )
    settings = request.app.state.settings
    return JobResponse(
        job_id=job.id,
        job_type=job.job_type,
        status=job.status,
        attempt=job.attempt,
        max_attempts=job.max_attempts,
        payload=job.payload,
        last_error=job.last_error,
        queued_at=job.queued_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        events=[
            JobEventItem(
                event_type=e.event_type,
                from_status=e.from_status,
                to_status=e.to_status,
                detail=e.detail,
                created_at=e.created_at,
            )
            for e in events
        ],
        poll_hint=f"GET {settings.public_base_url}/api/v1/jobs/{job.id}",
    )


@router.post(
    "/{job_id}/cancel",
    summary="取消任务",
    description=(
        "排队中的任务立即取消；运行中的任务记为 CANCEL_REQUESTED，"
        "在下一个处理步骤检查点进入 CANCELLED。"
    ),
    response_model=CancelJobResponse,
)
def cancel_job(
    job_id: Annotated[int, Path(description="任务 ID")],
    session: Annotated[Session, Depends(_get_session)],
) -> CancelJobResponse:
    """请求取消；排队任务立即取消，运行中任务在下一个处理步骤检查点停止。"""
    jobs = JobService(session)
    job = jobs.request_cancel(jobs.get_required(job_id))
    if job.asset_id is not None and job.status is JobStatus.CANCELLED:
        AssetService(session).mark_cancelled(job.asset_id)
    return CancelJobResponse(job_id=job.id, status=job.status)
