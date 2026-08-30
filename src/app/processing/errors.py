"""处理任务错误分类。

分类决定重试策略（架构不变量）：
- TransientError：基础设施瞬时故障（网络/MinIO/数据库），指数退避自动重试；
- DeterministicError：数据损坏、非法参数等确定性错误，不得盲目重试；
- NeedsInputError：缺少 CRS 等可人工补充信息，进入 NEEDS_INPUT 暂停。
"""


class TransientError(Exception):
    """瞬时基础设施错误，可按退避策略重试。"""


class DeterministicError(Exception):
    """确定性错误：重试不会成功，应直接失败并保留诊断。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class NeedsInputError(Exception):
    """缺少可人工补充的信息（如 CRS），任务进入 NEEDS_INPUT。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class ProcessingCancelledError(Exception):
    """任务在显式取消检查点停止；Job 已由检查点推进到 CANCELLED。"""
