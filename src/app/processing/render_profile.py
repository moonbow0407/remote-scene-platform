"""栅格默认渲染推断（纯函数）。

规则（首版，确定性）：
- 波段数 ≥ 3：RGB，取前三个波段；
- 波段数 1–2：灰度，取第 1 波段。
旧系统硬编码 {16,17,18} 的行为不复刻。
"""

from typing import Any, TypedDict


class RenderProfile(TypedDict):
    mode: str
    bands: list[int]


def infer_render_profile(band_count: int) -> RenderProfile:
    if band_count < 1:
        raise ValueError(f"波段数必须 ≥ 1，收到 {band_count}")
    if band_count >= 3:
        return RenderProfile(mode="rgb", bands=[1, 2, 3])
    return RenderProfile(mode="grayscale", bands=[1])


def render_profile_summary(profile: RenderProfile) -> dict[str, Any]:
    """供 API 输出的简要渲染信息。"""
    return {"mode": profile["mode"], "bands": profile["bands"]}
