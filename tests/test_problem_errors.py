"""RFC 9457 错误映射行为：problem+json 形状、稳定错误码与 trace_id 扩展。"""


def test_problem_error_shape(client) -> None:
    response = client.get("/test/problem", headers={"X-Request-ID": "trace-123"})
    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "TEST_CONFLICT"
    assert body["status"] == 409
    assert body["title"] == "测试冲突"
    assert body["detail"] == "测试冲突详情"
    assert body["trace_id"] == "trace-123"


def test_validation_error_returns_problem(client) -> None:
    response = client.get("/test/page", params={"page_size": "abc"})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "REQUEST_VALIDATION"
    assert body["errors"]


def test_page_size_over_limit_rejected(client) -> None:
    response = client.get("/test/page", params={"page_size": 1000})
    assert response.status_code == 422


def test_unhandled_exception_returns_500_problem(client) -> None:
    response = client.get("/test/unhandled")
    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert "trace_id" in body


def test_not_found_is_problem_json(client) -> None:
    response = client.get("/no/such/route")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "HTTP_404"
