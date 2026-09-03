"""数据源种子与编号规则。"""

from app.data_sources.seed_data import kind_of_code, seed_data_sources
from app.imagery.enums import RecordKind


def test_seed_covers_sentinel_and_uav_imagery() -> None:
    items = seed_data_sources()
    by_code = {item["code"]: item for item in items}
    assert by_code["000114"]["name"] == "哨兵二号"
    assert by_code["000114"]["kind"] == RecordKind.SATELLITE.value
    assert by_code["000201"]["kind"] == RecordKind.UAV.value
    assert "000203" not in by_code
    assert "000206" not in by_code


def test_kind_of_code() -> None:
    assert kind_of_code("000114") is RecordKind.SATELLITE
    assert kind_of_code("000201") is RecordKind.UAV
