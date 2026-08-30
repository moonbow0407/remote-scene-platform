"""Job 路由：进度轮询（首版不提供 SSE，事件模型为二期预留边界）。"""

import logging
from collections.abc import Iterator
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.assets.service import AssetService
from app.db import session_scope
from app.jobs.enums import JobStatus
from app.jobs.models import JobEvent
from app.jobs.service import JobService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["任务"])


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


@router.get("/{job_id}")
def get_job(
    job_id: UUID, request: Request, session: Annotated[Session, Depends(_get_session)]
) -> dict[str, Any]:
    service = JobService(session)
    job = service.get_required(job_id)
    events = session.scalars(
        sa.select(JobEvent)
        .where(JobEvent.job_id == job_id)
        .order_by(JobEvent.created_at)
        .limit(200)
    )
    settings = request.app.state.settings
    return {
        "job_id": str(job.id),
        "job_type": job.job_type.value,
        "status": job.status.value,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "payload": job.payload,
        "last_error": job.last_error,
        "queued_at": job.queued_at.isoformat() if job.queued_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "events": [
            {
                "event_type": e.event_type,
                "from_status": e.from_status.value if e.from_status else None,
                "to_status": e.to_status.value if e.to_status else None,
                "detail": e.detail,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ],
        "_poll_hint": f"GET {settings.public_base_url}/api/v1/jobs/{job.id}",
    }


@router.post("/{job_id}/cancel")
def cancel_job(
    job_id: UUID, session: Annotated[Session, Depends(_get_session)]
) -> dict[str, str]:
    """请求取消；排队任务立即取消，运行中任务在下一个处理步骤检查点停止。"""
    jobs = JobService(session)
    job = jobs.request_cancel(jobs.get_required(job_id))
    if job.asset_version_id is not None and job.status is JobStatus.CANCELLED:
        AssetService(session).mark_version_cancelled(job.asset_version_id)
    return {"job_id": str(job.id), "status": job.status.value}
