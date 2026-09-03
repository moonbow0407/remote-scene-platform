"""矿山请求模型与空间范围契约。"""

import pytest
from pydantic import ValidationError

from app.mines.schemas import MineCreate, MineUpdate


def _geometry() -> dict[str, object]:
    return {
        "type": "Polygon",
        "coordinates": [
            [[110.0, 40.0], [110.1, 40.0], [110.1, 40.1], [110.0, 40.0]],
        ],
    }


def test_create_accepts_reference_boundary_polygon_alias() -> None:
    mine = MineCreate(
        mine_id="M001",
        mine_name="示例矿山",
        boundary_polygon=_geometry(),
        mine_elevation_lower=1000,
        mine_elevation_upper=1200,
    )

    assert mine.spatial_geojson == _geometry()


def test_rejects_inverted_elevation_range() -> None:
    with pytest.raises(ValidationError, match="最高海拔不能小于最低海拔"):
        MineCreate(
            mine_id="M001",
            mine_name="示例矿山",
            spatial_geojson=_geometry(),
            mine_elevation_lower=1200,
            mine_elevation_upper=1000,
        )


def test_update_allows_a_partial_change() -> None:
    assert MineUpdate(mine_status=1).mine_status == 1
