"""栅格输入格式文件头校验。"""

import pytest

from app.processing.raster_ingestion import is_supported_tiff_magic


@pytest.mark.parametrize(
    "magic",
    [
        b"II*\x00",
        b"MM\x00*",
        b"II+\x00",
        b"MM\x00+",
    ],
)
def test_classic_tiff_and_bigtiff_magics_are_supported(magic: bytes) -> None:
    assert is_supported_tiff_magic(magic)


@pytest.mark.parametrize("magic", [b"", b"PNG\r", b"GIF8", b"RIFF"])
def test_non_tiff_magics_are_rejected(magic: bytes) -> None:
    assert not is_supported_tiff_magic(magic)
