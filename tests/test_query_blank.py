"""GET 空查询参数视为未传；JSON body 仍按类型校验。"""

from typing import Annotated

from fastapi import Depends, FastAPI, Query
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from app.imagery.enums import RecordKind, RecordStatus
from app.imagery.schemas import SearchRequest
from app.pagination import PageParams
from app.query import BlankAsNone, blank_as_default, blank_as_none


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    def probe(
        pagination: Annotated[PageParams, Depends()],
        name: Annotated[str | None, BlankAsNone, Query()] = None,
        data_source_id: Annotated[int | None, BlankAsNone, Query()] = None,
        kind: Annotated[RecordKind | None, BlankAsNone, Query()] = None,
        status: Annotated[RecordStatus | None, BlankAsNone, Query()] = None,
        deleted: Annotated[bool, blank_as_default(False), Query()] = False,
    ) -> dict[str, object]:
        return {
            "name": name,
            "data_source_id": data_source_id,
            "kind": None if kind is None else kind.value,
            "status": None if status is None else status.value,
            "deleted": deleted,
            "page": pagination.page,
            "page_size": pagination.page_size,
        }

    return app


def test_blank_as_none_helper() -> None:
    assert blank_as_none("") is None
    assert blank_as_none("   ") is None
    assert blank_as_none(None) is None
    assert blank_as_none("READY") == "READY"
    assert blank_as_none(3) == 3


def test_blank_as_default_helper() -> None:
    adapter = TypeAdapter(Annotated[int, blank_as_default(20)])
    assert adapter.validate_python("") == 20
    assert adapter.validate_python("  ") == 20
    assert adapter.validate_python(None) == 20
    assert adapter.validate_python("3") == 3


def test_empty_query_params_are_omitted() -> None:
    client = TestClient(_probe_app())
    response = client.get(
        "/probe?name=&data_source_id=&kind=&status=%20%20&deleted=&page=&page_size="
    )
    assert response.status_code == 200
    assert response.json() == {
        "name": None,
        "data_source_id": None,
        "kind": None,
        "status": None,
        "deleted": False,
        "page": 1,
        "page_size": 20,
    }


def test_present_query_params_still_parse() -> None:
    client = TestClient(_probe_app())
    response = client.get(
        "/probe",
        params={
            "data_source_id": "7",
            "kind": "SATELLITE",
            "status": "READY",
            "deleted": "true",
            "page": "2",
            "page_size": "50",
            "name": "哨兵",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "name": "哨兵",
        "data_source_id": 7,
        "kind": "SATELLITE",
        "status": "READY",
        "deleted": True,
        "page": 2,
        "page_size": 50,
    }


def test_invalid_query_values_still_422() -> None:
    client = TestClient(_probe_app())
    assert client.get("/probe", params={"data_source_id": "abc"}).status_code == 422
    assert client.get("/probe", params={"status": "NOPE"}).status_code == 422
    assert client.get("/probe", params={"page": "0"}).status_code == 422
    assert client.get("/probe", params={"page_size": "201"}).status_code == 422


def test_json_body_empty_string_is_still_invalid() -> None:
    try:
        SearchRequest.model_validate({"data_source_id": ""})
    except ValidationError as exc:
        assert any("data_source_id" in str(err.get("loc")) for err in exc.errors())
    else:
        raise AssertionError("empty string in JSON body should not coerce to None")
