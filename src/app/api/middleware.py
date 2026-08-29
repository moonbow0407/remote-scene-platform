"""请求中间件：trace_id 绑定、结构化访问日志与基线指标。

trace_id 取自请求头 `X-Request-ID`（允许网关注入），否则生成 UUIDv7；
响应回写同名头，使前端与日志可按同一标识串联问题。
"""

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.api.metrics import HTTP_REQUEST_COUNT, HTTP_REQUEST_DURATION
from app.ids import new_uuid7
from app.logging import trace_id_var

logger = logging.getLogger("app.access")


class TraceAccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        trace_id = request.headers.get("x-request-id") or str(new_uuid7())
        token = trace_id_var.set(trace_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000
            # 命中路由时使用路由模板聚合，避免 404/高频路径造成指标基数膨胀
            route = request.scope.get("route")
            path = getattr(route, "path", None) or "unmatched"
            status = getattr(response, "status_code", 500)
            HTTP_REQUEST_COUNT.labels(method=request.method, path=path, status=str(status)).inc()
            HTTP_REQUEST_DURATION.labels(method=request.method, path=path).observe(
                duration_ms / 1000
            )
            logger.info(
                "请求完成",
                extra={
                    "method": request.method,
                    "path": path,
                    "status": status,
                    "duration_ms": round(duration_ms, 2),
                },
            )
            trace_id_var.reset(token)
        response.headers["x-request-id"] = trace_id
        return response
