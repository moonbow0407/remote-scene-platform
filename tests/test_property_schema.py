"""资产 properties JSON Schema 校验与要素属性结构推断。"""

import pytest

from app.assets.enums import AssetType
from app.assets.property_schema import (
    DEFAULT_PROPERTY_SCHEMAS,
    accumulate_property_schema,
    infer_property_schema,
    json_value_type,
    property_schema_from_collected,
    validate_properties,
)
from app.errors import ProblemError
from app.jobs.enums import TASK_BY_JOB_TYPE, JobType


def test_default_schema_allows_acquired_at_and_extra_keys() -> None:
    validate_properties(
        DEFAULT_PROPERTY_SCHEMAS[AssetType.VECTOR],
        {"acquired_at": "2026-08-29T00:00:00+08:00", "mine": "A"},
    )


def test_schema_rejects_wrong_type() -> None:
    schema = {
        "type": "object",
        "properties": {"acquired_at": {"type": "string"}},
        "additionalProperties": False,
    }
    with pytest.raises(ProblemError) as exc:
        validate_properties(schema, {"acquired_at": 1})
    assert exc.value.status == 422


def test_infer_property_schema_collects_union_types() -> None:
    rows = [
        {"name": "a", "value": 1, "note": None},
        {"name": "b", "value": 2, "note": "x"},
    ]
    schema = infer_property_schema(rows)
    by_name = {item["name"]: item["types"] for item in schema}
    assert by_name["name"] == ["string"]
    assert by_name["value"] == ["integer"]
    assert by_name["note"] == ["null", "string"]
    assert json_value_type(True) == "boolean"

    collected: dict[str, set[str]] = {}
    for row in rows:
        accumulate_property_schema(collected, row)
    assert property_schema_from_collected(collected) == schema


def test_job_type_maps_to_celery_task_name() -> None:
    assert TASK_BY_JOB_TYPE[JobType.RASTER_INGESTION] == "processing.ingest_raster"
    assert TASK_BY_JOB_TYPE[JobType.VECTOR_INGESTION] == "processing.ingest_vector"
    assert TASK_BY_JOB_TYPE[JobType.ATTACHMENT_INGESTION] == "processing.ingest_attachment"
