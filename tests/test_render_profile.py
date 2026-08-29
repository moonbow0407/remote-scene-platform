"""渲染推断纯函数行为。"""

import pytest

from app.processing.render_profile import infer_render_profile


@pytest.mark.parametrize("band_count", [3, 4, 8])
def test_multiband_infers_rgb_first_three(band_count: int) -> None:
    profile = infer_render_profile(band_count)
    assert profile == {"mode": "rgb", "bands": [1, 2, 3]}


@pytest.mark.parametrize("band_count", [1, 2])
def test_one_or_two_bands_infers_grayscale(band_count: int) -> None:
    profile = infer_render_profile(band_count)
    assert profile == {"mode": "grayscale", "bands": [1]}


def test_zero_bands_rejected() -> None:
    with pytest.raises(ValueError):
        infer_render_profile(0)
