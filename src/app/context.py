"""ActorContext：操作者上下文接缝。

首版不实现鉴权，Service 统一使用匿名系统操作者；二期接入 JWT 与 ADMIN/USER 时，
只需在此处替换解析逻辑并把真实用户标识写入 owner_id/created_by，
不需要改造 API 与任务主链路。
"""

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class ActorContext:
    """当前操作者。actor_id 为 None 表示匿名系统操作者。"""

    actor_id: str | None
    display_name: str


_ANONYMOUS = ActorContext(actor_id=None, display_name="anonymous-system")


def get_actor() -> ActorContext:
    """返回当前操作者；首版固定为匿名系统操作者。"""
    return _ANONYMOUS


def now_utc() -> datetime:
    """统一时钟入口：全部业务时间以 UTC 生成并携带时区。"""
    return datetime.now(UTC)
