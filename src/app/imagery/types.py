"""卫星/无人机行类型别名。"""

from app.imagery.enums import RecordKind
from app.imagery.models import SatelliteData, UavData

RasterRecord = SatelliteData | UavData

RECORD_MODELS: dict[RecordKind, type[RasterRecord]] = {
    RecordKind.SATELLITE: SatelliteData,
    RecordKind.UAV: UavData,
}

RECORD_LABEL: dict[RecordKind, str] = {
    RecordKind.SATELLITE: "卫星",
    RecordKind.UAV: "无人机",
}


def record_cls(kind: RecordKind) -> type[RasterRecord]:
    return RECORD_MODELS[kind]


def object_prefix(kind: RecordKind, record_id: int) -> str:
    folder = "satellites" if kind is RecordKind.SATELLITE else "uavs"
    return f"{folder}/{record_id}"
