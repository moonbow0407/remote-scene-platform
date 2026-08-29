"""GeoPackage 几何二进制（GP 头 + WKB）编解码。不依赖 GDAL/Fiona。"""

from __future__ import annotations

import struct

GP_MAGIC = b"GP"


def encode_geometry(wkb: bytes, srs_id: int) -> bytes:
    """小端、无 envelope 的 GeoPackageBinary。"""
    return GP_MAGIC + bytes((0, 0x01)) + struct.pack("<i", srs_id) + wkb


def decode_geometry(blob: bytes) -> tuple[int, bytes]:
    """返回 (srs_id, wkb)。"""
    if len(blob) < 8 or blob[:2] != GP_MAGIC:
        raise ValueError("不是 GeoPackage 几何二进制")
    flags = blob[3]
    little = (flags & 0x01) == 1
    envelope = (flags >> 1) & 0x07
    endian = "<" if little else ">"
    srs_id = int(struct.unpack(endian + "i", blob[4:8])[0])
    offset = 8
    envelope_bytes = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}
    offset += envelope_bytes.get(envelope, 0)
    return srs_id, blob[offset:]
