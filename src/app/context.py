"""ActorContext：操作者上下文接缝。

未接入鉴权的业务模块继续通过 `get_actor()` 获取匿名系统操作者。
鉴权模块把已认证用户映射为本结构（actor_id / display_name / role），
业务 Service 只依赖 ActorContext，不解析 JWT 或 User ORM。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class ActorRole(StrEnum):
    """首版角色：仅管理员与普通用户，不引入权限字符串或策略引擎。"""

    ADMIN = "ADMIN"
    USER = "USER"


@dataclass(frozen=True)
class ActorContext:
    """当前操作者。actor_id 为 None 表示匿名系统操作者。"""

    actor_id: str | None
    display_name: str
    role: ActorRole | None = None


_ANONYMOUS = ActorContext(actor_id=None, display_name="anonymous-system")


def get_actor() -> ActorContext:
    """返回匿名系统操作者。真实用户身份由 auth 依赖注入，不在此解析请求。"""
    return _ANONYMOUS


def now_utc() -> datetime:
    """统一时钟入口：全部业务时间以 UTC 生成并携带时区。"""
    return datetime.now(UTC)
