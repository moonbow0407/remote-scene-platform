"""错误模型与 RFC 9457 (application/problem+json) 映射。

约定：
- 每个业务错误携带稳定 `code`（机器可判）与中文 `title/detail`（人可读）；
- 所有 problem 响应附 `trace_id`，与日志链路对齐；
- 不把业务失败包装成 HTTP 200。
"""


class ProblemError(Exception):
    """业务/基础设施错误的统一载体，由 API 层转换为 RFC 9457 响应。"""

    def __init__(self, *, status: int, code: str, title: str, detail: str | None = None) -> None:
        super().__init__(detail or title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail


def not_found(resource: str, identifier: object) -> ProblemError:
    return ProblemError(
        status=404,
        code=f"{resource.upper()}_NOT_FOUND",
        title=f"{resource} 不存在",
        detail=f"标识 {identifier} 对应的 {resource} 不存在",
    )


def conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(status=409, code=code, title="资源状态冲突", detail=detail)


def validation_error(detail: str) -> ProblemError:
    return ProblemError(
        status=422, code="DOMAIN_VALIDATION_ERROR", title="请求不满足业务规则", detail=detail
    )


def service_unavailable(component: str, detail: str) -> ProblemError:
    return ProblemError(
        status=503,
        code="DEPENDENCY_UNAVAILABLE",
        title=f"依赖组件 {component} 不可用",
        detail=detail,
    )
