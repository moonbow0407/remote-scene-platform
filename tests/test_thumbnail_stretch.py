"""缩略图拉伸必须忽略 NaN / Inf / NaN NoData。"""

import pytest

from app.processing.raster_ingestion import stretch_band_to_uint8

np = pytest.importorskip("numpy")


def test_stretch_ignores_nan_inf_and_finite_nodata() -> None:
    band = np.array([[0.0, 10.0], [np.nan, np.inf], [-np.inf, 999.0]], dtype=np.float32)
    out = stretch_band_to_uint8(band, nodata=999.0)
    assert out.dtype == np.uint8
    assert out[0, 0] == 0
    assert out[0, 1] == 255
    assert out[1, 0] == 0
    assert out[1, 1] == 0
    assert out[2, 0] == 0
    assert out[2, 1] == 0


def test_stretch_nan_nodata_does_not_keep_nan_in_range() -> None:
    band = np.array([np.nan, 2.0, 4.0], dtype=np.float32)
    out = stretch_band_to_uint8(band, nodata=float("nan"))
    assert out[0] == 0
    assert out[1] == 0
    assert out[2] == 255


def test_stretch_all_invalid_returns_zeros() -> None:
    band = np.array([np.nan, np.inf, -np.inf], dtype=np.float32)
    out = stretch_band_to_uint8(band, nodata=None)
    assert np.array_equal(out, np.zeros(3, dtype=np.uint8))
