"""请求追踪行为：X-Request-ID 透传与 UUIDv7 生成。"""

import uuid


def test_incoming_request_id_is_echoed(client) -> None:
    response = client.get("/api/v1/healthz", headers={"X-Request-ID": "trace-abc"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "trace-abc"


def test_missing_request_id_is_generated_as_uuidv7(client) -> None:
    response = client.get("/api/v1/healthz")
    header_value = response.headers["x-request-id"]
    parsed = uuid.UUID(header_value)
    assert parsed.version == 7
