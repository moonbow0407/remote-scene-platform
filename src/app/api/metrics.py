"""Prometheus 基线指标。

Stage 1 只暴露请求计数与延迟直方图；队列、Job、存储类指标随对应能力落地（Stage 6）。
指标对象为模块级单例，避免进程内重复注册。
"""

from prometheus_client import Counter, Histogram

HTTP_REQUEST_COUNT = Counter(
    "http_requests_total",
    "HTTP 请求总数",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求耗时（秒）",
    ["method", "path"],
)
