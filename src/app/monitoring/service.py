"""监测服务：计划 CRUD、occurrence 幂等派发、增量选择与不可变输入快照。

分层与事务边界：
- 本服务不开启事务，由调用方（API 请求 / Scheduler tick）通过 session_scope
  显式提交；occurrence、Run、输入快照与派发动作在同一次 flush 中落库，
  由调用方事务保证原子；
- 与 Job 基础设施的分工：本模块决定"什么时候执行、执行什么"（调度、occurrence
  唯一性、增量选择、快照冻结），"怎么可靠执行"（Job 状态机、Outbox、重试、
  租约）归 jobs/processing 模块；二者仅通过 `RunDispatcher` 接缝衔接；
- Scheduler 进程层互斥（pg advisory lock）在 scheduler/main.py，本服务不感知
  进程数量——即使锁失效，occurrence 的数据库唯一约束仍保证不重复派发。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

import sqlalchemy as sa
from geoalchemy2 import WKTElement
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.assets.geometry import GeometryValidationError, geojson_to_wkt
from app.catalogs.service import CatalogService
from app.context import ActorContext, get_actor, now_utc
from app.ecology.service import EcologyService
from app.errors import ProblemError, conflict, not_found, validation_error
from app.monitoring.dispatch import JobRunDispatcher
from app.monitoring.enums import (
    OccurrenceStatus,
    OccurrenceTrigger,
    PlanStatus,
    RunStatus,
)
from app.monitoring.models import (
    MonitoringOccurrence,
    MonitoringPlan,
    MonitoringPlanParameter,
    MonitoringRun,
    MonitoringRunInput,
)
from app.monitoring.scheduling import Schedule, ScheduleScanLimitExceeded, parse_schedule
from app.monitoring.schemas import PlanCreate, PlanUpdate
from app.monitoring.selection import SelectionCriteria, select_ready_assets
from app.pagination import Page, PageParams

logger = logging.getLogger(__name__)


def _actor_uuid() -> int | None:
    actor = get_actor()
    if actor.actor_id is None:
        return None
    try:
        return int(actor.actor_id)
    except ValueError:
        return None


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"调度时间必须携带时区，收到 naive datetime：{value!r}")
    return value.astimezone(UTC)


def _from_db(value: datetime) -> datetime:
    """数据库回读的 timestamptz 统一按 UTC 解释。

    PostgreSQL/psycopg 回读 timestamptz 恒为 aware；naive 只会出现在 SQLite
    单元测试方言（其 DATETIME 丢弃 tzinfo，写入的即 UTC 值）。此处不放宽
    scheduling 层的严格校验，只在服务与数据库的边界做方言归一。
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class RunDispatcher(Protocol):
    """执行派发接缝：MonitoringRun → Job + Outbox → Dispatcher → RabbitMQ。

    生产实现为 `app.monitoring.dispatch.JobRunDispatcher`（复用
    `JobService.create_job_with_outbox` 同事务创建任务与投递事件）；
    禁止直接 `celery.send_task` 或自行发布 RabbitMQ 消息。occurrence/Run/
    快照与 Job+Outbox 的原子性由"同一 Session 同一事务"保证。
    """

    def dispatch(
        self, session: Session, run: MonitoringRun, input_version_ids: list[int]
    ) -> int | None:
        """创建执行任务并返回 job_id；返回 None 仅供测试替身表示"未派发"。"""
        ...


@dataclass(frozen=True)
class PlanView:
    """计划列表/详情的派生视图数据（生态参数主键集合）。"""

    ecological_parameter_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class RunView:
    """执行的派生视图数据（occurrence 冗余信息与输入数量）。"""

    scheduled_for: datetime
    trigger: OccurrenceTrigger
    input_count: int


@dataclass
class TickSummary:
    """一次调度扫描的结果摘要（可变累加器），供 Scheduler 记录结构化日志。"""

    plans_considered: int = 0
    dispatched: int = 0
    missed_recorded: int = 0
    skipped_plan_ids: list[int] = field(default_factory=list)


def _dedupe_preserving_order(ids: Sequence[int]) -> list[int]:
    return list(dict.fromkeys(ids))


class MonitoringService:
    def asset_has_snapshot_references(self, asset_id: int) -> bool:
        """资产是否仍被不可变监测输入快照引用；生命周期模块据此禁止物理清理。"""
        return bool(
            self._session.scalar(
                sa.select(sa.func.count())
                .select_from(MonitoringRunInput)
                .where(MonitoringRunInput.asset_id == asset_id)
            )
        )

    def __init__(self, session: Session, dispatcher: RunDispatcher | None = None) -> None:
        self._session = session
        # 默认生产派发器：同事务创建 MONITORING_RUN Job + Outbox 事件；
        # 测试经参数注入替身（Recording/Failing 等）
        self._dispatcher = dispatcher or JobRunDispatcher()

    # ---- 计划 CRUD ----

    def create_plan(self, body: PlanCreate, *, actor: ActorContext | None = None) -> MonitoringPlan:
        boundary, boundary_wkt = self._normalize_boundary(body.boundary)
        schedule = parse_schedule(body.schedule_type, body.schedule_expression, body.timezone)
        if body.category_id is not None:
            CatalogService(self._session).get_required(body.category_id)
        parameter_ids = _dedupe_preserving_order(body.ecological_parameter_ids)
        ecology = EcologyService(self._session)
        for parameter_id in parameter_ids:
            ecology.get_parameter_required(parameter_id)

        operator = actor or get_actor()
        now = now_utc()
        plan = MonitoringPlan(
            name=body.name.strip(),
            status=PlanStatus.ACTIVE,
            boundary=boundary,
            boundary_wkt=boundary_wkt,
            schedule_type=body.schedule_type,
            schedule_expression=body.schedule_expression,
            timezone=body.timezone,
            category_id=body.category_id,
            created_by=None if operator.actor_id is None else int(operator.actor_id),
        )
        # 首个 occurrence：以创建时刻锚定新网格，不立即触发
        plan.next_run_at = schedule.next_after(now, anchor=now)
        self._session.add(plan)
        self._session.flush()
        self._replace_parameters(plan, parameter_ids)
        return plan

    def get_plan(self, plan_id: int) -> MonitoringPlan | None:
        return self._session.get(MonitoringPlan, plan_id)

    def get_plan_required(self, plan_id: int) -> MonitoringPlan:
        plan = self.get_plan(plan_id)
        if plan is None:
            raise not_found("监测计划", plan_id)
        return plan

    def list_plans(
        self, params: PageParams, *, status: PlanStatus | None = None
    ) -> Page[MonitoringPlan]:
        stmt = sa.select(MonitoringPlan)
        count_stmt = sa.select(sa.func.count()).select_from(MonitoringPlan)
        if status is not None:
            stmt = stmt.where(MonitoringPlan.status == status)
            count_stmt = count_stmt.where(MonitoringPlan.status == status)
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(MonitoringPlan.created_at.desc())
                .offset(params.offset)
                .limit(params.limit)
            )
        )
        return Page.build(rows, total, params)

    def update_plan(self, plan_id: int, body: PlanUpdate) -> MonitoringPlan:
        plan = self.get_plan_required(plan_id)
        data = body.model_dump(exclude_unset=True)

        if data.get("name") is not None:
            plan.name = data["name"]
        if data.get("boundary") is not None:
            boundary, boundary_wkt = self._normalize_boundary(data["boundary"])
            plan.boundary = boundary
            plan.boundary_wkt = boundary_wkt
        if "schedule_type" in data or "schedule_expression" in data or "timezone" in data:
            # 调度三字段作为整体校验（未出现的字段沿用现值），变更后从当前时刻
            # 锚定新网格：调度变更立即生效，不沿旧网格补发
            schedule = parse_schedule(
                data.get("schedule_type", plan.schedule_type),
                data.get("schedule_expression", plan.schedule_expression),
                data.get("timezone", plan.timezone),
            )
            plan.schedule_type = data.get("schedule_type", plan.schedule_type)
            plan.schedule_expression = data.get("schedule_expression", plan.schedule_expression)
            plan.timezone = data.get("timezone", plan.timezone)
            now = now_utc()
            plan.next_run_at = schedule.next_after(now, anchor=now)
        if "category_id" in data:
            if data["category_id"] is None:
                plan.category_id = None
            else:
                CatalogService(self._session).get_required(data["category_id"])
                plan.category_id = data["category_id"]
        if "ecological_parameter_ids" in data:
            parameter_ids = _dedupe_preserving_order(data["ecological_parameter_ids"] or [])
            ecology = EcologyService(self._session)
            for parameter_id in parameter_ids:
                ecology.get_parameter_required(parameter_id)
            self._replace_parameters(plan, parameter_ids)

        self._session.flush()
        return plan

    def pause_plan(self, plan_id: int) -> MonitoringPlan:
        plan = self.get_plan_required(plan_id)
        if plan.status is not PlanStatus.ACTIVE:
            raise conflict(
                code="MONITORING_PLAN_STATUS_INVALID",
                detail=f"计划 {plan_id} 当前状态为 {plan.status}，仅 ACTIVE 计划可暂停",
            )
        plan.status = PlanStatus.PAUSED
        # next_run_at 保留为网格指针：PAUSED 期间不参与调度，恢复时据此续接
        # 原周期网格（暂停期间的错过周期不补录，见 resume_plan）
        self._session.flush()
        return plan

    def resume_plan(self, plan_id: int) -> MonitoringPlan:
        plan = self.get_plan_required(plan_id)
        if plan.status is not PlanStatus.PAUSED:
            raise conflict(
                code="MONITORING_PLAN_STATUS_INVALID",
                detail=f"计划 {plan_id} 当前状态为 {plan.status}，仅 PAUSED 计划可恢复",
            )
        schedule = self._schedule_of(plan)
        anchor = plan.next_run_at if plan.next_run_at is not None else now_utc()
        # 从当前时刻计算下一个网格点：暂停期间的 occurrence 一律不补录
        # （PAUSED 计划不在调度范围内），网格本身不因暂停漂移
        plan.next_run_at = schedule.next_after(now_utc(), anchor=_from_db(anchor))
        plan.status = PlanStatus.ACTIVE
        self._session.flush()
        return plan

    def delete_plan(self, plan_id: int) -> None:
        """物理删除计划（关联 occurrence/Run/快照随数据库级联删除）。

        软删除与 7 天恢复期按阶段方案属 Stage 6 统一生命周期，本阶段不引入。
        """
        plan = self.get_plan_required(plan_id)
        self._session.delete(plan)
        self._session.flush()

    def describe_plans(self, plans: Sequence[MonitoringPlan]) -> dict[int, PlanView]:
        """批量取计划的生态参数主键集合，避免列表接口 N+1。"""
        plan_ids = [plan.id for plan in plans]
        if not plan_ids:
            return {}
        rows = list(
            self._session.scalars(
                sa.select(MonitoringPlanParameter).where(
                    MonitoringPlanParameter.plan_id.in_(plan_ids)
                )
            )
        )
        views: dict[int, PlanView] = {}
        for row in rows:
            views.setdefault(row.plan_id, PlanView(ecological_parameter_ids=[]))
            views[row.plan_id].ecological_parameter_ids.append(row.ecological_parameter_id)
        for plan_id in plan_ids:
            views.setdefault(plan_id, PlanView(ecological_parameter_ids=[]))
        return views

    # ---- 手动触发与调度派发 ----

    def trigger_plan(self, plan_id: int) -> MonitoringRun:
        """手动触发一次执行（occurrence 时刻为当前时刻）。

        手动触发不受 ACTIVE/PAUSED 限制（操作者显式动作优先于调度暂停），
        但同样受 occurrence 唯一约束保护：与调度竞争时同一计划时刻只会产生
        一次执行。
        """
        plan = self.get_plan_required(plan_id)
        now = now_utc()
        occurrence = self._insert_occurrence(
            plan_id=plan.id,
            scheduled_for=now,
            trigger=OccurrenceTrigger.MANUAL,
            status=OccurrenceStatus.DISPATCHED,
        )
        if occurrence is None:
            raise conflict(
                code="MONITORING_OCCURRENCE_DUPLICATE",
                detail=f"计划 {plan.id} 在该时刻已存在执行记录，未重复创建",
            )
        return self._create_run(plan, occurrence, selection_now=now)

    def process_due_plans(self, *, now: datetime) -> TickSummary:
        """调度扫描：为全部到期 ACTIVE 计划生成 occurrence 并派发执行。

        停机补跑语义：窗口 [next_run_at, now] 内除最近一次外的 occurrence 记录为
        MISSED（仅审计，不创建 Run/任务），最近一次正常派发——即使连续错过多个
        周期也只产生一次执行，避免任务风暴。next_run_at 推进到最新网格点之后。
        """
        now = _ensure_aware_utc(now)
        plans = list(
            self._session.scalars(
                sa.select(MonitoringPlan)
                .where(
                    MonitoringPlan.status == PlanStatus.ACTIVE,
                    MonitoringPlan.next_run_at.is_not(None),
                    MonitoringPlan.next_run_at <= now,
                )
                .order_by(MonitoringPlan.next_run_at)
            )
        )
        summary = TickSummary(plans_considered=len(plans))
        for plan in plans:
            assert plan.next_run_at is not None  # 查询条件保证
            try:
                schedule = self._schedule_of(plan)
                window_start = _from_db(plan.next_run_at)
                occurrences = schedule.occurrences_between(window_start, now, anchor=window_start)
            except (ScheduleScanLimitExceeded, ProblemError) as exc:
                # 已持久化的计划解析/枚举失败属于数据异常：显式失败并保留诊断，
                # 不推进 next_run_at，下一轮继续暴露
                logger.error(
                    "计划 %s 调度规则处理失败，跳过本轮扫描：%s",
                    plan.id,
                    exc,
                    extra={"plan_id": str(plan.id)},
                )
                summary.skipped_plan_ids.append(plan.id)
                continue
            if not occurrences:
                # 理论不可达（窗口下界即锚点网格点）；防御数据异常导致空窗口
                continue
            for missed_time in occurrences[:-1]:
                created = self._insert_occurrence(
                    plan_id=plan.id,
                    scheduled_for=missed_time,
                    trigger=OccurrenceTrigger.SCHEDULED,
                    status=OccurrenceStatus.MISSED,
                )
                if created is not None:
                    summary.missed_recorded += 1
            occurrence = self._insert_occurrence(
                plan_id=plan.id,
                scheduled_for=occurrences[-1],
                trigger=OccurrenceTrigger.SCHEDULED,
                status=OccurrenceStatus.DISPATCHED,
            )
            if occurrence is not None:
                self._create_run(plan, occurrence, selection_now=now)
                summary.dispatched += 1
            plan.next_run_at = schedule.next_after(
                occurrences[-1], anchor=_from_db(plan.next_run_at)
            )
        return summary

    # ---- 执行查询与状态推进 ----

    def list_runs(
        self, plan_id: int, params: PageParams, *, status: RunStatus | None = None
    ) -> Page[MonitoringRun]:
        self.get_plan_required(plan_id)
        stmt = sa.select(MonitoringRun).where(MonitoringRun.plan_id == plan_id)
        count_stmt = (
            sa.select(sa.func.count())
            .select_from(MonitoringRun)
            .where(MonitoringRun.plan_id == plan_id)
        )
        if status is not None:
            stmt = stmt.where(MonitoringRun.status == status)
            count_stmt = count_stmt.where(MonitoringRun.status == status)
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(MonitoringRun.created_at.desc())
                .offset(params.offset)
                .limit(params.limit)
            )
        )
        return Page.build(rows, total, params)

    def get_run_required(self, run_id: int) -> MonitoringRun:
        run = self._session.get(MonitoringRun, run_id)
        if run is None:
            raise not_found("监测执行", run_id)
        return run

    def list_run_inputs(self, run_id: int, params: PageParams) -> Page[MonitoringRunInput]:
        self.get_run_required(run_id)
        stmt = sa.select(MonitoringRunInput).where(MonitoringRunInput.run_id == run_id)
        count_stmt = (
            sa.select(sa.func.count())
            .select_from(MonitoringRunInput)
            .where(MonitoringRunInput.run_id == run_id)
        )
        total = int(self._session.scalar(count_stmt) or 0)
        rows = list(
            self._session.scalars(
                stmt.order_by(MonitoringRunInput.created_at)
                .offset(params.offset)
                .limit(params.limit)
            )
        )
        return Page.build(rows, total, params)

    def describe_runs(self, runs: Sequence[MonitoringRun]) -> dict[int, RunView]:
        """批量取执行的 occurrence 冗余信息与输入数量，避免列表接口 N+1。"""
        if not runs:
            return {}
        run_ids = [run.id for run in runs]
        occurrences = {
            row.id: row
            for row in self._session.scalars(
                sa.select(MonitoringOccurrence).where(
                    MonitoringOccurrence.id.in_([run.occurrence_id for run in runs])
                )
            )
        }
        input_counts: dict[int, int] = {}
        count_rows = self._session.execute(
            sa.select(MonitoringRunInput.run_id, sa.func.count())
            .where(MonitoringRunInput.run_id.in_(run_ids))
            .group_by(MonitoringRunInput.run_id)
        )
        for run_id, count in count_rows:
            input_counts[run_id] = int(count)
        views: dict[int, RunView] = {}
        for run in runs:
            occurrence = occurrences.get(run.occurrence_id)
            if occurrence is None:
                raise not_found("监测 occurrence", run.occurrence_id)
            views[run.id] = RunView(
                scheduled_for=occurrence.scheduled_for,
                trigger=occurrence.trigger,
                input_count=input_counts.get(run.id, 0),
            )
        return views

    def mark_run_started(self, run_id: int) -> MonitoringRun:
        """执行方开始处理（PENDING → RUNNING）。由监测执行方经接缝调用。

        幂等：至少一次投递下，租约回收后的重试尝试会再次到达本方法；执行已
        处于 RUNNING（上次尝试已标记开始）视为已满足，直接返回，不报状态冲突。
        """
        run = self.get_run_required(run_id)
        if run.status is RunStatus.PENDING:
            run.status = RunStatus.RUNNING
            run.started_at = now_utc()
        elif run.status is not RunStatus.RUNNING:
            raise conflict(
                code="MONITORING_RUN_STATE_INVALID",
                detail=f"监测执行 {run_id} 当前状态为 {run.status}，要求为 PENDING/RUNNING",
            )
        self._session.flush()
        return run

    def mark_run_succeeded(self, run_id: int) -> MonitoringRun:
        """执行成功（RUNNING → SUCCEEDED）。

        成功同时把计划 last_successful_run_at 推进到本次 occurrence 的计划时刻；
        增量窗口锚点不在此处推进——下一次执行的选择窗口直接读取最近一次
        SUCCEEDED Run 的 window_anchor，单一事实来源。
        """
        run = self._require_run_in_status(run_id, RunStatus.RUNNING)
        run.status = RunStatus.SUCCEEDED
        run.finished_at = now_utc()
        occurrence = self._session.get(MonitoringOccurrence, run.occurrence_id)
        plan = self._session.get(MonitoringPlan, run.plan_id)
        if occurrence is not None and plan is not None:
            plan.last_successful_run_at = occurrence.scheduled_for
        self._session.flush()
        return run

    def mark_run_failed(
        self, run_id: int, *, detail: str, code: str = "MONITORING_RUN_FAILED"
    ) -> MonitoringRun:
        """执行失败（RUNNING → FAILED）；code 区分失败类别（如快照损坏 SNAPSHOT_BROKEN）。"""
        run = self._require_run_in_status(run_id, RunStatus.RUNNING)
        run.status = RunStatus.FAILED
        run.finished_at = now_utc()
        run.diagnostics = {"code": code, "detail": detail}
        self._session.flush()
        return run

    # ---- 内部实现 ----

    def _require_run_in_status(self, run_id: int, expected: RunStatus) -> MonitoringRun:
        run = self.get_run_required(run_id)
        if run.status is not expected:
            raise conflict(
                code="MONITORING_RUN_STATE_INVALID",
                detail=f"监测执行 {run_id} 当前状态为 {run.status}，要求为 {expected}",
            )
        return run

    def _schedule_of(self, plan: MonitoringPlan) -> Schedule:
        return parse_schedule(plan.schedule_type, plan.schedule_expression, plan.timezone)

    def _normalize_boundary(self, geojson: dict[str, object]) -> tuple[WKTElement, str]:
        """校验 GeoJSON 并归一化为 MULTIPOLYGON WKT（存储与选择共用一种形态）。"""
        normalized = dict(geojson)
        if normalized.get("type") == "Polygon":
            normalized["type"] = "MultiPolygon"
            normalized["coordinates"] = [normalized["coordinates"]]
        try:
            wkt = geojson_to_wkt(normalized)
        except GeometryValidationError as exc:
            raise validation_error(f"计划边界不合法：{exc}") from exc
        return WKTElement(wkt, srid=4326), wkt

    def _replace_parameters(self, plan: MonitoringPlan, parameter_ids: list[int]) -> None:
        """整体替换计划的生态参数关联（调用方已校验存在性）。"""
        existing = {row.ecological_parameter_id: row for row in plan.parameters}
        for parameter_id in parameter_ids:
            if parameter_id not in existing:
                self._session.add(
                    MonitoringPlanParameter(
                        plan_id=plan.id,
                        ecological_parameter_id=parameter_id,
                    )
                )
        for parameter_id, row in existing.items():
            if parameter_id not in parameter_ids:
                self._session.delete(row)
        self._session.flush()

    def _insert_occurrence(
        self,
        *,
        plan_id: int,
        scheduled_for: datetime,
        trigger: OccurrenceTrigger,
        status: OccurrenceStatus,
    ) -> MonitoringOccurrence | None:
        """插入 occurrence；撞 (plan_id, scheduled_for) 唯一约束时返回 None。

        幂等由数据库唯一约束兜底（SAVEPOINT 回滚仅撤销本次插入）：多实例
        Scheduler、重复扫描、重启恢复、手动触发与调度竞争在此收敛。
        """
        occurrence = MonitoringOccurrence(
            plan_id=plan_id,
            scheduled_for=scheduled_for,
            trigger=trigger,
            status=status,
        )
        try:
            with self._session.begin_nested():
                self._session.add(occurrence)
                self._session.flush()
        except IntegrityError:
            return None
        return occurrence

    def _create_run(
        self, plan: MonitoringPlan, occurrence: MonitoringOccurrence, *, selection_now: datetime
    ) -> MonitoringRun:
        """创建执行并冻结输入快照（occurrence 已存在，二者同事务提交）。"""
        criteria = SelectionCriteria(
            boundary_wkt=plan.boundary_wkt,
            category_id=plan.category_id,
            ecological_parameter_ids=tuple(row.ecological_parameter_id for row in plan.parameters),
            window_anchor=self._last_successful_anchor(plan.id),
        )
        versions = select_ready_assets(self._session, criteria)
        run = MonitoringRun(
            plan_id=plan.id,
            occurrence_id=occurrence.id,
            status=RunStatus.PENDING,
            # 窗口锚点=本次选择时刻：创建于该时刻之后的版本落入下一窗口，
            # 任何版本至多被选中一次；失败 Run 不推进锚点（见 window_anchor 注释）
            window_anchor=selection_now,
        )
        self._session.add(run)
        self._session.flush()
        for asset in versions:
            self._session.add(
                MonitoringRunInput(
                    run_id=run.id,
                    asset_id=asset.id,
                )
            )
        self._session.flush()
        run.job_id = self._dispatcher.dispatch(
            self._session, run, [asset.id for asset in versions]
        )
        logger.info(
            "监测执行已创建并冻结输入快照",
            extra={
                "run_id": str(run.id),
                "plan_id": str(plan.id),
                "occurrence_id": str(occurrence.id),
                "scheduled_for": occurrence.scheduled_for.isoformat(),
                "input_count": len(versions),
            },
        )
        return run

    def _last_successful_anchor(self, plan_id: int) -> datetime | None:
        """增量窗口下界 = 最近一次 SUCCEEDED Run 的选择时刻；无则全量选择。"""
        return self._session.scalar(
            sa.select(MonitoringRun.window_anchor)
            .where(
                MonitoringRun.plan_id == plan_id,
                MonitoringRun.status == RunStatus.SUCCEEDED,
            )
            .order_by(MonitoringRun.window_anchor.desc())
            .limit(1)
        )
