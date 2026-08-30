"""资产版本状态机：与 Job 状态机同理由 Service 唯一执行转换。

UPLOADING 仅在上传会话未完成时存在（版本行尚未创建，故数据库中实际从 VALIDATING 起）；
DELETED 由 Stage 6 软删除流程引入。
"""

from app.assets.enums import AssetVersionStatus

ALLOWED_VERSION_TRANSITIONS: dict[AssetVersionStatus, frozenset[AssetVersionStatus]] = {
    AssetVersionStatus.UPLOADING: frozenset(
        {AssetVersionStatus.VALIDATING, AssetVersionStatus.DELETED}
    ),
    AssetVersionStatus.VALIDATING: frozenset(
        {
            AssetVersionStatus.PROCESSING,
            # 验证期缺 CRS/地理定位即暂停等待补充（A2.5 断点恢复），不算失败
            AssetVersionStatus.NEEDS_INPUT,
            AssetVersionStatus.FAILED,
            AssetVersionStatus.DELETED,
        }
    ),
    AssetVersionStatus.PROCESSING: frozenset(
        {AssetVersionStatus.READY, AssetVersionStatus.FAILED, AssetVersionStatus.NEEDS_INPUT}
    ),
    AssetVersionStatus.NEEDS_INPUT: frozenset(
        {
            AssetVersionStatus.PROCESSING,
            AssetVersionStatus.FAILED,
            AssetVersionStatus.DELETED,
        }
    ),
    AssetVersionStatus.READY: frozenset({AssetVersionStatus.DELETED}),
    AssetVersionStatus.FAILED: frozenset({AssetVersionStatus.DELETED}),
    AssetVersionStatus.DELETED: frozenset(),
}


def is_version_transition_allowed(current: AssetVersionStatus, target: AssetVersionStatus) -> bool:
    return target in ALLOWED_VERSION_TRANSITIONS[current]
