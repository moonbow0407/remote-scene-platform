"""监测模块路由：HTTP 适配层。

Plan 与 Run 的查询/操作均经 MonitoringService；boundary 以 GeoJSON 进出，
WKT/GeoJSON 转换在服务端完成（ST_AsGeoJSON），客户端不接触 WKT。
"""

from collections.abc import Iterator
from json import loads as json_loads
from typing import Annotated, Any
from uuid import UUID

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy.orm import Session

from app.context import get_actor
from app.db import session_scope
from app.monitoring.enums import PlanStatus, RunStatus
from app.monitoring.models import MonitoringPlan, MonitoringRun, MonitoringRunInput
from app.monitoring.schemas import (
    PlanCreate,
    PlanDetailResponse,
    PlanSummaryResponse,
    PlanUpdate,
    RunInputResponse,
    RunResponse,
    RunTransitionRequest,
)
from app.monitoring.service import MonitoringService, PlanView, RunView
from app.pagination import Page, PageParams

router = APIRouter(prefix="/monitoring", tags=["监测"])


def _get_session(request: Request) -> Iterator[Session]:
    with session_scope(request.app.state.session_factory) as session:
        yield session


def _get_service(session: Annotated[Session, Depends(_get_session)]) -> MonitoringService:
    return MonitoringService(session)


def _plan_summary(plan: MonitoringPlan, view: PlanView) -> PlanSummaryResponse:
    return PlanSummaryResponse(
        id=plan.id,
        name=plan.name,
        status=plan.status,
        schedule_type=plan.schedule_type,
        schedule_expression=plan.schedule_expression,
        timezone=plan.timezone,
        resource_catalog_id=plan.resource_catalog_id,
        ecological_parameter_ids=view.ecological_parameter_ids,
        next_run_at=plan.next_run_at,
        last_successful_run_at=plan.last_successful_run_at,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
    )


def _plan_detail(session: Session, plan: MonitoringPlan, view: PlanView) -> PlanDetailResponse:
    boundary_geojson: dict[str, Any] | None = None
    raw = session.execute(sa.select(sa.func.ST_AsGeoJSON(plan.boundary))).scalar()
    if raw:
        boundary_geojson = json_loads(raw)
    return PlanDetailResponse(
        **_plan_summary(plan, view).model_dump(),
        boundary_geojson=boundary_geojson,
    )


def _run_response(run: MonitoringRun, view: RunView) -> RunResponse:
    return RunResponse(
        id=run.id,
        plan_id=run.plan_id,
        occurrence_id=run.occurrence_id,
        scheduled_for=view.scheduled_for,
        status=run.status,
        window_anchor=run.window_anchor,
        job_id=run.job_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        diagnostics=run.diagnostics,
        trigger=view.trigger,
        input_count=view.input_count,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _run_input_response(row: MonitoringRunInput) -> RunInputResponse:
    return RunInputResponse.model_validate(row, from_attributes=True)


@router.get(
    "/plans",
    summary="监测计划列表",
    response_model=Page[PlanSummaryResponse],
)
def list_plans(
    params: Annotated[PageParams, Depends()],
    session: Annotated[Session, Depends(_get_session)],
    service: Annotated[MonitoringService, Depends(_get_service)],
    status: Annotated[
        PlanStatus | None, Query(description="计划状态：ACTIVE 调度中、PAUSED 已暂停；省略不过滤")
    ] = None,
) -> Page[PlanSummaryResponse]:
    page = service.list_plans(params, status=status)
    views = service.describe_plans(page.items)
    return Page.build(
        [_plan_summary(plan, views[plan.id]) for plan in page.items], page.total, params
    )


@router.get(
    "/plans/{plan_id}",
    summary="监测计划详情",
    description="含空间范围 GeoJSON 与调度配置。",
    response_model=PlanDetailResponse,
)
def get_plan(
    plan_id: Annotated[UUID, Path(description="监测计划 ID")],
    session: Annotated[Session, Depends(_get_session)],
    service: Annotated[MonitoringService, Depends(_get_service)],
) -> PlanDetailResponse:
    plan = service.get_plan_required(plan_id)
    views = service.describe_plans([plan])
    return _plan_detail(session, plan, views[plan.id])


@router.post(
    "/plans",
    status_code=201,
    summary="创建监测计划",
    description=(
        "空间范围必须是 EPSG:4326 的 Polygon 或 MultiPolygon。创建后由独立 Scheduler 扫描到期计划。"
    ),
    response_model=PlanDetailResponse,
)
def create_plan(
    body: PlanCreate,
    session: Annotated[Session, Depends(_get_session)],
    service: Annotated[MonitoringService, Depends(_get_service)],
) -> PlanDetailResponse:
    get_actor()
    plan = service.create_plan(body)
    views = service.describe_plans([plan])
    return _plan_detail(session, plan, views[plan.id])


@router.put(
    "/plans/{plan_id}",
    summary="更新监测计划",
    description="未出现的字段保持不变。生态参数列表若出现则整体替换。",
    response_model=PlanDetailResponse,
)
def update_plan(
    plan_id: Annotated[UUID, Path(description="监测计划 ID")],
    body: PlanUpdate,
    session: Annotated[Session, Depends(_get_session)],
    service: Annotated[MonitoringService, Depends(_get_service)],
) -> PlanDetailResponse:
    get_actor()
    plan = service.update_plan(plan_id, body)
    views = service.describe_plans([plan])
    return _plan_detail(session, plan, views[plan.id])


@router.delete(
    "/plans/{plan_id}",
    status_code=204,
    summary="删除监测计划",
)
def delete_plan(
    plan_id: Annotated[UUID, Path(description="监测计划 ID")],
    service: Annotated[MonitoringService, Depends(_get_service)],
) -> None:
    get_actor()
    service.delete_plan(plan_id)


@router.post(
    "/plans/{plan_id}/pause",
    summary="暂停监测计划",
    description="暂停后不再参与调度，配置保留。",
    response_model=PlanSummaryResponse,
)
def pause_plan(
    plan_id: Annotated[UUID, Path(description="监测计划 ID")],
    service: Annotated[MonitoringService, Depends(_get_service)],
) -> PlanSummaryResponse:
    get_actor()
    plan = service.pause_plan(plan_id)
    views = service.describe_plans([plan])
    return _plan_summary(plan, views[plan.id])


@router.post(
    "/plans/{plan_id}/resume",
    summary="恢复监测计划",
    description="从 PAUSED 恢复为 ACTIVE，重新参与调度。",
    response_model=PlanSummaryResponse,
)
def resume_plan(
    plan_id: Annotated[UUID, Path(description="监测计划 ID")],
    service: Annotated[MonitoringService, Depends(_get_service)],
) -> PlanSummaryResponse:
    get_actor()
    plan = service.resume_plan(plan_id)
    views = service.describe_plans([plan])
    return _plan_summary(plan, views[plan.id])


@router.post(
    "/plans/{plan_id}/trigger",
    status_code=201,
    summary="手动触发一次执行",
    description="立即按增量窗口选择 READY 资产版本，冻结输入快照并派发任务。",
    response_model=RunResponse,
)
def trigger_plan(
    plan_id: Annotated[UUID, Path(description="监测计划 ID")],
    service: Annotated[MonitoringService, Depends(_get_service)],
) -> RunResponse:
    get_actor()
    run = service.trigger_plan(plan_id)
    views = service.describe_runs([run])
    return _run_response(run, views[run.id])


@router.get(
    "/plans/{plan_id}/runs",
    summary="计划执行记录",
    response_model=Page[RunResponse],
)
def list_runs(
    plan_id: Annotated[UUID, Path(description="监测计划 ID")],
    params: Annotated[PageParams, Depends()],
    service: Annotated[MonitoringService, Depends(_get_service)],
    status: Annotated[
        RunStatus | None,
        Query(description="执行状态：PENDING / RUNNING / SUCCEEDED / FAILED；省略不过滤"),
    ] = None,
) -> Page[RunResponse]:
    page = service.list_runs(plan_id, params, status=status)
    views = service.describe_runs(page.items)
    return Page.build([_run_response(run, views[run.id]) for run in page.items], page.total, params)


@router.get(
    "/runs/{run_id}",
    summary="监测执行详情",
    response_model=RunResponse,
)
def get_run(
    run_id: Annotated[UUID, Path(description="监测执行 ID")],
    service: Annotated[MonitoringService, Depends(_get_service)],
) -> RunResponse:
    run = service.get_run_required(run_id)
    views = service.describe_runs([run])
    return _run_response(run, views[run.id])


@router.get(
    "/runs/{run_id}/inputs",
    summary="执行输入快照",
    description="本次执行冻结的资产版本清单，创建后不可变。",
    response_model=Page[RunInputResponse],
)
def list_run_inputs(
    run_id: Annotated[UUID, Path(description="监测执行 ID")],
    params: Annotated[PageParams, Depends()],
    service: Annotated[MonitoringService, Depends(_get_service)],
) -> Page[RunInputResponse]:
    page = service.list_run_inputs(run_id, params)
    return Page.build([_run_input_response(row) for row in page.items], page.total, params)


@router.post(
    "/runs/{run_id}/start",
    summary="标记执行开始",
    description="执行方接缝：PENDING → RUNNING。日常联调一般由 Worker 调用，不必手点。",
    response_model=RunResponse,
)
def start_run(
    run_id: Annotated[UUID, Path(description="监测执行 ID")],
    service: Annotated[MonitoringService, Depends(_get_service)],
) -> RunResponse:
    """执行方接缝：标记执行开始（PENDING → RUNNING）。"""
    run = service.mark_run_started(run_id)
    views = service.describe_runs([run])
    return _run_response(run, views[run.id])


@router.post(
    "/runs/{run_id}/succeed",
    summary="标记执行成功",
    description="执行方接缝：RUNNING → SUCCEEDED，并更新计划的最近成功时刻。",
    response_model=RunResponse,
)
def succeed_run(
    run_id: Annotated[UUID, Path(description="监测执行 ID")],
    service: Annotated[MonitoringService, Depends(_get_service)],
) -> RunResponse:
    """执行方接缝：标记执行成功（RUNNING → SUCCEEDED），推进计划最近成功时刻。"""
    run = service.mark_run_succeeded(run_id)
    views = service.describe_runs([run])
    return _run_response(run, views[run.id])


@router.post(
    "/runs/{run_id}/fail",
    summary="标记执行失败",
    description="执行方接缝：RUNNING → FAILED，并把诊断写入执行记录。",
    response_model=RunResponse,
)
def fail_run(
    run_id: Annotated[UUID, Path(description="监测执行 ID")],
    body: RunTransitionRequest,
    service: Annotated[MonitoringService, Depends(_get_service)],
) -> RunResponse:
    """执行方接缝：标记执行失败（RUNNING → FAILED），记录诊断。"""
    run = service.mark_run_failed(run_id, detail=body.detail or "未提供失败诊断")
    views = service.describe_runs([run])
    return _run_response(run, views[run.id])
