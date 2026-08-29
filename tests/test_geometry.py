"""GeoJSON 几何校验与 WKT 转换行为。"""

import pytest

from app.assets.geometry import GeometryValidationError, geojson_to_wkt


def test_valid_polygon_converted() -> None:
    wkt = geojson_to_wkt(
        {
            "type": "Polygon",
            "coordinates": [[[114.0, 30.0], [115.0, 30.0], [115.0, 31.0], [114.0, 30.0]]],
        }
    )
    assert wkt == "POLYGON((114.0 30.0, 115.0 30.0, 115.0 31.0, 114.0 30.0))"


def test_valid_multipolygon_converted() -> None:
    wkt = geojson_to_wkt(
        {
            "type": "MultiPolygon",
            "coordinates": [
                [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]],
                [[[2.0, 2.0], [3.0, 2.0], [3.0, 3.0], [2.0, 2.0]]],
            ],
        }
    )
    assert wkt.startswith("MULTIPOLYGON(((0.0 0.0")
    assert wkt.endswith(")))")
    assert ")), ((" in wkt


@pytest.mark.parametrize(
    "geometry",
    [
        {"type": "Point", "coordinates": [1, 2]},
        {"type": "Polygon", "coordinates": []},
        {"type": "Polygon", "coordinates": [[[1.0, 2.0]]]},
        {"type": "Polygon", "coordinates": [[["x", "y"], [1, 2], [2, 3], [1, 2]]]},
        {"type": "Polygon", "coordinates": [[[True, 2.0], [1, 2], [2, 3], [True, 2.0]]]},
        {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1]]]},
        {"type": "Polygon", "coordinates": [[[181, 0], [1, 0], [1, 1], [181, 0]]]},
        {"type": "Polygon", "coordinates": [[[0, 91], [1, 0], [1, 1], [0, 91]]]},
        {"type": "Polygon", "coordinates": [[[0, 0, 1], [1, 0], [1, 1], [0, 0, 1]]]},
        "not-a-dict",
    ],
)
def test_invalid_geometry_rejected(geometry: object) -> None:
    with pytest.raises(GeometryValidationError):
        geojson_to_wkt(geometry)  # type: ignore[arg-type]
