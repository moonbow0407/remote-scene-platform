"""数据源种子。编号与大系统 SatelliteTypeMapping / 无人机产品类型对齐。"""

from __future__ import annotations

from typing import TypedDict

from app.imagery.enums import RecordKind

SATELLITE_PREFIX = "0001"
UAV_PREFIX = "0002"
CODE_PATTERN = r"^\d{6}$"


class SeedDataSource(TypedDict):
    code: str
    name: str
    kind: str


_SATELLITES: tuple[tuple[str, str], ...] = (
    ("000101", "矿大南湖号"),
    ("000102", "珠海一号"),
    ("000103", "陆探一号"),
    ("000104", "吉林一号"),
    ("000105", "吉林一号（多光谱）"),
    ("000106", "高分一号"),
    ("000107", "高分二号"),
    ("000108", "高分三号"),
    ("000109", "高分四号"),
    ("000110", "高分五号"),
    ("000111", "高分六号"),
    ("000112", "高分七号"),
    ("000113", "哨兵一号"),
    ("000114", "哨兵二号"),
    ("000115", "LandSat5"),
    ("000116", "LandSat7"),
    ("000117", "LandSat8"),
    ("000118", "LandSat9"),
)

_UAVS: tuple[tuple[str, str], ...] = (
    ("000201", "无人机多光谱"),
    ("000202", "无人机高光谱"),
)


def seed_data_sources() -> list[SeedDataSource]:
    items: list[SeedDataSource] = []
    for code, name in _SATELLITES:
        items.append({"code": code, "name": name, "kind": RecordKind.SATELLITE.value})
    for code, name in _UAVS:
        items.append({"code": code, "name": name, "kind": RecordKind.UAV.value})
    return items


def kind_of_code(code: str) -> RecordKind:
    prefix = code[:4]
    if prefix == SATELLITE_PREFIX:
        return RecordKind.SATELLITE
    if prefix == UAV_PREFIX:
        return RecordKind.UAV
    raise ValueError(f"无法从编号 {code} 判断种类")
