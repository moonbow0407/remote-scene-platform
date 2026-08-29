"""统一分页基元行为与分页参数校验。"""


def test_page_build_shape(client) -> None:
    response = client.get("/test/page", params={"page": 3, "page_size": 10})
    assert response.status_code == 200
    body = response.json()
    assert body["page"] == 3
    assert body["page_size"] == 10
    assert body["total"] == 100
    assert body["items"] == list(range(20, 30))


def test_default_pagination(client) -> None:
    response = client.get("/test/page")
    body = response.json()
    assert body["page"] == 1
    assert body["page_size"] == 20
    assert body["items"][0] == 0
