"""ActorContext：操作者上下文接缝。

HTTP 请求由鉴权依赖把已认证用户绑定到 ContextVar；
Worker / Scheduler / Cleanup 未绑定时 `get_actor()` 返回匿名系统操作者。
业务 Service 只依赖 ActorContext，不解析 JWT 或 User ORM。
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from app.schema_docs import enum_docs


@enum_docs("角色", "ADMIN：管理员；USER：普通用户。")
class ActorRole(StrEnum):
    """登录用户角色。"""

    ADMIN = "ADMIN"
    USER = "USER"


@dataclass(frozen=True)
class ActorContext:
    """当前操作者。actor_id 为 None 表示匿名系统操作者。"""

    actor_id: str | None
    display_name: str
    role: ActorRole | None = None


_ANONYMOUS = ActorContext(actor_id=None, display_name="anonymous-system")
_actor_var: ContextVar[ActorContext] = ContextVar("actor", default=_ANONYMOUS)


def get_actor() -> ActorContext:
    """当前操作者。HTTP 鉴权依赖绑定真实用户；后台进程保持匿名。"""
    return _actor_var.get()


@contextmanager
def bind_actor(actor: ActorContext) -> Iterator[None]:
    """在代码块内绑定操作者；离开时恢复原值。"""
    token = _actor_var.set(actor)
    try:
        yield
    finally:
        _actor_var.reset(token)


def now_utc() -> datetime:
    """统一时钟入口：全部业务时间以 UTC 生成并携带时区。"""
    return datetime.now(UTC)
