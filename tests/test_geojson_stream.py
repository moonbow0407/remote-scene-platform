"""GeoJSON 流式解析：按块读取，不把 FeatureCollection 整文件 json.loads。"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from app.processing.errors import DeterministicError
from app.processing.geojson_stream import (
    JsonStream,
    iter_geojson_feature_objects,
    peek_geojson_root_type,
)


def test_peek_root_type_feature_collection(tmp_path: Path) -> None:
    path = tmp_path / "fc.geojson"
    path.write_text(
        '{"type":"FeatureCollection","features":[{"type":"Feature","geometry":null,"properties":{}}]}',
        encoding="utf-8",
    )
    assert peek_geojson_root_type(path) == "FeatureCollection"


def test_peek_root_type_feature(tmp_path: Path) -> None:
    path = tmp_path / "f.geojson"
    path.write_text(
        '{"type":"Feature","geometry":{"type":"Point","coordinates":[0,0]},"properties":{}}',
        encoding="utf-8",
    )
    assert peek_geojson_root_type(path) == "Feature"


def test_peek_features_first_does_not_require_scanning_array(tmp_path: Path) -> None:
    path = tmp_path / "late_type.geojson"
    path.write_text(
        '{"features":[{"type":"Feature","geometry":null,"properties":{}}],"type":"FeatureCollection"}',
        encoding="utf-8",
    )
    assert peek_geojson_root_type(path) == "FeatureCollection"


def test_iter_feature_collection_and_single_feature(tmp_path: Path) -> None:
    collection = tmp_path / "fc.geojson"
    collection.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [114.0, 30.0]},
                        "properties": {"name": "a"},
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [115.0, 31.0]},
                        "properties": {"name": "b"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    items = list(iter_geojson_feature_objects(collection))
    assert [item["properties"]["name"] for item in items] == ["a", "b"]

    single = tmp_path / "one.geojson"
    single.write_text(
        json.dumps(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [1.0, 2.0]},
                "properties": {"name": "solo"},
            }
        ),
        encoding="utf-8",
    )
    solo = list(iter_geojson_feature_objects(single))
    assert len(solo) == 1
    assert solo[0]["properties"]["name"] == "solo"


def test_json_stream_number_across_chunk_boundary() -> None:
    payload = '{"n":12345,"ok":true}'
    stream = JsonStream(io.StringIO(payload), chunk_size=4)
    assert stream.peek() == "{"
    stream.consume("{")
    assert stream.read_json_value() == "n"
    stream.consume(":")
    assert stream.read_json_value() == 12345
    stream.consume(",")
    assert stream.read_json_value() == "ok"
    stream.consume(":")
    assert stream.read_json_value() is True
    stream.consume("}")


def test_skip_value_does_not_materialize_array() -> None:
    payload = '{"skip":[' + ",".join(["1"] * 1000) + '],"type":"FeatureCollection"}'
    stream = JsonStream(io.StringIO(payload), chunk_size=16)
    stream.consume("{")
    assert stream.read_json_value() == "skip"
    stream.consume(":")
    stream.skip_value()
    stream.consume(",")
    assert stream.read_json_value() == "type"
    stream.consume(":")
    assert stream.read_json_value() == "FeatureCollection"


def test_iter_rejects_non_feature_member(tmp_path: Path) -> None:
    path = tmp_path / "bad.geojson"
    path.write_text(
        '{"type":"FeatureCollection","features":[{"type":"Point","coordinates":[0,0]}]}',
        encoding="utf-8",
    )
    with pytest.raises(DeterministicError) as exc:
        list(iter_geojson_feature_objects(path))
    assert exc.value.code == "INVALID_VECTOR_ARCHIVE"
