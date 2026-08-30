"""调度规则解析与周期计算单元测试（纯逻辑，无数据库）。

覆盖：固定间隔/RRULE 解析校验、next_run_at 计算、timezone 语义、
补跑窗口枚举与上限、网格锚点不变量。
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.errors import ProblemError
from app.monitoring.enums import ScheduleType
from app.monitoring.scheduling import (
    MAX_CATCHUP_OCCURRENCES,
    Schedule,
    ScheduleScanLimitExceeded,
    parse_schedule,
)


class TestIntervalSchedule:
    def test_parse_and_next_after(self) -> None:
        schedule = parse_schedule(ScheduleType.INTERVAL, "PT6H", "UTC")
        anchor = datetime(2026, 8, 30, 0, 0, tzinfo=UTC)
        assert schedule.next_after(anchor, anchor=anchor) == anchor + timedelta(hours=6)
        # 落在周期中点的 after：下一个网格点对齐锚点，不产生漂移
        middle = anchor + timedelta(hours=2)
        assert schedule.next_after(middle, anchor=anchor) == anchor + timedelta(hours=6)
        # after 恰为网格点：严格晚于该点
        assert schedule.next_after(
            anchor + timedelta(hours=6), anchor=anchor
        ) == anchor + timedelta(hours=12)

    def test_interval_minimum_enforced(self) -> None:
        with pytest.raises(ProblemError):
            parse_schedule(ScheduleType.INTERVAL, "PT30S", "UTC")

    def test_interval_invalid_expression(self) -> None:
        for expression in ("", "6h", "PT", "P1M", "every day"):
            with pytest.raises(ProblemError):
                parse_schedule(ScheduleType.INTERVAL, expression, "UTC")

    def test_occurrences_between_and_catch_up_cap(self) -> None:
        schedule = parse_schedule(ScheduleType.INTERVAL, "P1D", "UTC")
        anchor = datetime(2026, 8, 1, tzinfo=UTC)
        window = schedule.occurrences_between(anchor, anchor + timedelta(days=3), anchor=anchor)
        assert window == [anchor + timedelta(days=i) for i in range(4)]

        far_end = anchor + timedelta(days=MAX_CATCHUP_OCCURRENCES + 10)
        with pytest.raises(ScheduleScanLimitExceeded):
            schedule.occurrences_between(anchor, far_end, anchor=anchor)


class TestRruleSchedule:
    def test_daily_byhour_respects_timezone(self) -> None:
        # 上海时区每天 09:00 → UTC 01:00；锚点本身取上海 09:00 对应 UTC 时刻
        schedule = parse_schedule(
            ScheduleType.RRULE, "FREQ=DAILY;BYHOUR=9;BYMINUTE=0", "Asia/Shanghai"
        )
        anchor = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)  # 上海 2026-08-30 09:00
        nxt = schedule.next_after(anchor, anchor=anchor)
        assert nxt == datetime(2026, 8, 31, 1, 0, tzinfo=UTC)  # 上海 2026-08-31 09:00

    def test_utc_fallback_when_expression_has_no_time(self) -> None:
        schedule = parse_schedule(ScheduleType.RRULE, "FREQ=WEEKLY", "Asia/Shanghai")
        anchor = datetime(2026, 8, 30, 1, 0, tzinfo=UTC)
        assert schedule.next_after(anchor, anchor=anchor) == anchor + timedelta(weeks=1)

    def test_window_enumeration_bounded_by_window(self) -> None:
        schedule = parse_schedule(ScheduleType.RRULE, "FREQ=DAILY", "UTC")
        anchor = datetime(2026, 8, 1, tzinfo=UTC)
        window = schedule.occurrences_between(anchor, anchor + timedelta(days=2), anchor=anchor)
        assert window == [anchor, anchor + timedelta(days=1), anchor + timedelta(days=2)]

    def test_count_limited_rule_exhausts(self) -> None:
        schedule = parse_schedule(ScheduleType.RRULE, "FREQ=DAILY;COUNT=2", "UTC")
        anchor = datetime(2026, 8, 1, tzinfo=UTC)
        assert schedule.next_after(anchor + timedelta(days=2), anchor=anchor) is None

    def test_dtstart_rejected(self) -> None:
        with pytest.raises(ProblemError):
            parse_schedule(ScheduleType.RRULE, "DTSTART:20260101T000000Z\nFREQ=DAILY", "UTC")

    def test_secondly_freq_rejected(self) -> None:
        with pytest.raises(ProblemError):
            parse_schedule(ScheduleType.RRULE, "FREQ=SECONDLY", "UTC")

    def test_invalid_expression_rejected(self) -> None:
        with pytest.raises(ProblemError):
            parse_schedule(ScheduleType.RRULE, "FREQ=NOPE", "UTC")

    def test_catch_up_cap(self) -> None:
        schedule = parse_schedule(ScheduleType.RRULE, "FREQ=MINUTELY", "UTC")
        anchor = datetime(2026, 8, 1, tzinfo=UTC)
        with pytest.raises(ScheduleScanLimitExceeded):
            schedule.occurrences_between(
                anchor, anchor + timedelta(minutes=MAX_CATCHUP_OCCURRENCES + 5), anchor=anchor
            )


class TestTimezoneValidation:
    def test_invalid_timezone_rejected(self) -> None:
        with pytest.raises(ProblemError):
            parse_schedule(ScheduleType.INTERVAL, "P1D", "Mars/Olympus")

    def test_zoneinfo_accepted(self) -> None:
        schedule = parse_schedule(ScheduleType.INTERVAL, "P1D", "Pacific/Auckland")
        assert schedule.tz == ZoneInfo("Pacific/Auckland")


class TestAnchorReinvariant:
    def test_reanchor_at_grid_point_preserves_grid(self) -> None:
        """扫描路径以 next_run_at 重锚：网格集合不变（否则 occurrence 标识漂移）。"""
        rule_a = Schedule(
            schedule_type=ScheduleType.RRULE, expression="FREQ=DAILY", tz=ZoneInfo("UTC")
        )
        created = datetime(2026, 1, 1, tzinfo=UTC)
        first = rule_a.next_after(created, anchor=created)
        assert first == datetime(2026, 1, 2, tzinfo=UTC)
        assert first is not None
        # 以首个网格点重锚后，后续网格点与原网格一致
        second = rule_a.next_after(first, anchor=first)
        assert second == datetime(2026, 1, 3, tzinfo=UTC)
        # 原锚点下的同一结果（对照）
        assert rule_a.next_after(first, anchor=created) == second
