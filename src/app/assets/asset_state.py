"""资产状态机：只经 AssetService 转换。"""

from app.assets.enums import AssetStatus

ALLOWED_ASSET_TRANSITIONS: dict[AssetStatus, frozenset[AssetStatus]] = {
    AssetStatus.UPLOADING: frozenset({AssetStatus.VALIDATING, AssetStatus.FAILED}),
    AssetStatus.VALIDATING: frozenset(
        {
            AssetStatus.PROCESSING,
            AssetStatus.NEEDS_INPUT,
            AssetStatus.FAILED,
        }
    ),
    AssetStatus.PROCESSING: frozenset(
        {AssetStatus.READY, AssetStatus.FAILED, AssetStatus.NEEDS_INPUT}
    ),
    AssetStatus.NEEDS_INPUT: frozenset({AssetStatus.PROCESSING, AssetStatus.FAILED}),
    AssetStatus.READY: frozenset(),
    AssetStatus.FAILED: frozenset(),
}


def is_asset_transition_allowed(current: AssetStatus, target: AssetStatus) -> bool:
    return target in ALLOWED_ASSET_TRANSITIONS[current]
