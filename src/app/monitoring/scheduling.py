"""调度规则解析与周期计算（纯函数，无数据库依赖）。

设计约定：

- 计划支持固定间隔（ISO 8601 duration 受限子集）与 RFC 5545 RRULE 两种调度；
- 周期网格锚点（anchor）由调用方给出：计划创建/调度变更/手动恢复时用"当前时刻"，
  Scheduler 扫描时用已持久化的 `next_run_at`。`next_run_at` 恒为网格点，以其重锚
  不改变网格集合本身，同时把每次扫描的枚举开销限制在窗口大小内，而不是自计划
  创建起的全部历史（dateutil 的 rrule 只能从 dtstart 起迭代）；
- 全部 occurrence 时刻一律换算为 UTC 返回，保证 (plan_id, scheduled_for) 唯一
  标识的稳定性；RRULE 的时刻在计划时区生成后转 UTC，"每天 09:00" 永远落在
  计划时区的 09:00，而不是 UTC 的 09:00；
- 停机补跑窗口 [next_run_at, now] 内的 occurrence 有界枚举；超过上限视为异常
  （调度粒度过细叠加超长停机），由调用方显式失败并保留诊断，不做静默截断。
"""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr

from app.errors import validation_error
from app.monitoring.enums import ScheduleType

# 补跑枚举上限：超过即认为调度粒度与停机时长组合异常（如每分钟一次停机数月），
# 拒绝无限追补，由 Scheduler 记录错误诊断。只影响 MISSED 行数，不产生任务风暴。
MAX_CATCHUP_OCCURRENCES = 1000

# 固定间隔下限：防止过细粒度在停机后产生海量 MISSED 记录
MIN_INTERVAL_SECONDS = 60

# ISO 8601 duration 受限子集：P[n]W、P[n]D[T[n]H[n]M[n]S]，至少一个分量；
# 不支持年/月（天数不定，无法作为固定时长参与确定性计算）
_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)

# RRULE 字符串禁止携带 DTSTART：网格锚点由系统控制（创建/变更时刻或已持久化的
# next_run_at），客户端锚点会让 occurrence 标识在编辑后漂移
_FORBIDDEN_RRULE_KEYS = ("DTSTART",)

_RRULE_FREQ_PATTERN = re.compile(r"FREQ\s*=\s*([A-Z]+)", re.IGNORECASE)

_ALLOWED_FREQS = {"MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY"}


def validate_timezone(name: str) -> ZoneInfo:
    """校验 IANA 时区名并返回 ZoneInfo；非法时区在创建/更新计划时尽早失败。"""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise validation_error(f"时区 {name!r} 不是合法的 IANA 时区名称") from exc


def _ensure_utc(value: datetime) -> datetime:
    """调度计算只接受带时区的时刻；naive 输入是调用方 bug，显式失败。"""
    if value.tzinfo is None:
        raise ValueError(f"调度计算要求带时区的 UTC 时刻，收到 naive datetime：{value!r}")
    return value.astimezone(UTC)


class Schedule:
    """调度规则：可校验的表达式 + 时区，按锚点计算网格点。

    `anchor` 是网格起点：固定间隔的 occurrence = anchor + k*delta；RRULE 以
    anchor（换算到计划时区）为 dtstart 生成。调用方必须保证 anchor 本身是网格点
    （创建/变更时锚定当前时刻，扫描时锚定 next_run_at）。
    """

    def __init__(self, *, schedule_type: ScheduleType, expression: str, tz: ZoneInfo) -> None:
        self.schedule_type = schedule_type
        self.expression = expression
        self.tz = tz
        if schedule_type is ScheduleType.INTERVAL:
            self._interval = _parse_interval(expression)
            self._rrule: object | None = None
        else:
            self._interval = None
            # 用任意锚点先做一次解析，非法 RRULE 在计划创建/更新时即失败
            self._build_rrule(datetime.now(UTC))

    def _build_rrule(self, anchor_utc: datetime):
        local_anchor = _ensure_utc(anchor_utc).astimezone(self.tz)
        try:
            rule = rrulestr(self.expression, dtstart=local_anchor)
        except ValueError as exc:
            raise validation_error(f"RRULE 表达式不合法：{self.expression!r}（{exc}）") from exc
        return rule

    def next_after(self, after_utc: datetime, *, anchor: datetime) -> datetime | None:
        """返回严格晚于 after_utc 的下一个 occurrence（UTC）；周期耗尽返回 None。"""
        _ensure_utc(after_utc)
        if self._interval is not None:
            delta = self._interval
            base = _ensure_utc(anchor)
            if after_utc < base:
                # after 早于锚点：锚点本身即第一个网格点（正常调用路径 after>=anchor）
                return base
            steps = (after_utc - base) // delta + 1
            return base + steps * delta
        rule = self._build_rrule(anchor)
        nxt = rule.after(after_utc.astimezone(self.tz), inc=False)
        return None if nxt is None else nxt.astimezone(UTC)

    def occurrences_between(
        self, window_start_utc: datetime, window_end_utc: datetime, *, anchor: datetime
    ) -> list[datetime]:
        """枚举 [window_start, window_end] 内全部 occurrence（UTC，升序）。

        调用方约定 window_start 即网格锚点（Scheduler 扫描路径恒成立），因此
        枚举开销与窗口长度成正比，与计划历史长度无关。超过 MAX_CATCHUP_OCCURRENCES
        时抛出 ScheduleScanLimitExceeded，不做静默截断。
        """
        _ensure_utc(window_start_utc)
        _ensure_utc(window_end_utc)
        if window_end_utc < window_start_utc:
            return []
        if self._interval is not None:
            delta = self._interval
            base = _ensure_utc(anchor)
            first_step = max(0, -((base - window_start_utc) // delta))
            last_step = (window_end_utc - base) // delta
            count = last_step - first_step + 1
            if count > MAX_CATCHUP_OCCURRENCES:
                raise ScheduleScanLimitExceeded(
                    f"补跑窗口内 occurrence 数量 {count} 超过上限 {MAX_CATCHUP_OCCURRENCES}"
                )
            return [base + step * delta for step in range(first_step, last_step + 1)]

        rule = self._build_rrule(anchor)
        result: list[datetime] = []
        for occurrence in rule:
            occurrence_utc = occurrence.astimezone(UTC)
            if occurrence_utc > window_end_utc:
                break
            if occurrence_utc >= window_start_utc:
                result.append(occurrence_utc)
                if len(result) > MAX_CATCHUP_OCCURRENCES:
                    raise ScheduleScanLimitExceeded(
                        f"补跑窗口内 occurrence 数量超过上限 {MAX_CATCHUP_OCCURRENCES}"
                    )
        return result


class ScheduleScanLimitExceeded(Exception):
    """补跑窗口超出有界枚举上限；调用方必须显式失败并记录诊断。"""


def _parse_interval(expression: str) -> timedelta:
    match = _DURATION_PATTERN.fullmatch(expression.strip())
    if match is None:
        raise validation_error(
            f"固定间隔表达式 {expression!r} 不合法，"
            "须为 ISO 8601 duration 受限子集，如 PT6H、P1D、P1DT12H、P2W"
        )
    parts = match.groupdict()
    if all(value is None for value in parts.values()):
        raise validation_error(f"固定间隔表达式 {expression!r} 缺少时长分量")
    delta = timedelta(
        weeks=int(parts["weeks"] or 0),
        days=int(parts["days"] or 0),
        hours=int(parts["hours"] or 0),
        minutes=int(parts["minutes"] or 0),
        seconds=int(parts["seconds"] or 0),
    )
    if delta.total_seconds() < MIN_INTERVAL_SECONDS:
        raise validation_error(f"固定间隔至少为 {MIN_INTERVAL_SECONDS} 秒，收到 {expression!r}")
    return delta


def _validate_rrule_expression(expression: str) -> None:
    stripped = expression.strip()
    if any(key in stripped.upper() for key in _FORBIDDEN_RRULE_KEYS):
        raise validation_error(
            "RRULE 表达式不允许携带 DTSTART：周期网格锚点由系统按计划创建/变更时刻控制"
        )
    freq_match = _RRULE_FREQ_PATTERN.search(stripped)
    if freq_match is None:
        raise validation_error("RRULE 表达式缺少 FREQ 分量")
    if freq_match.group(1).upper() not in _ALLOWED_FREQS:
        raise validation_error(
            f"RRULE FREQ={freq_match.group(1)} 不支持，"
            f"允许的粒度：{'/'.join(sorted(_ALLOWED_FREQS))}"
        )


def parse_schedule(schedule_type: ScheduleType, expression: str, timezone_name: str) -> Schedule:
    """解析并校验调度规则；非法输入抛 422 ProblemError（复用 RFC 9457 错误体系）。"""
    tz = validate_timezone(timezone_name)
    if schedule_type is ScheduleType.RRULE:
        _validate_rrule_expression(expression)
    return Schedule(schedule_type=schedule_type, expression=expression, tz=tz)
