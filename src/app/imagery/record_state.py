"""影像状态机：只经 ImageryService 转换。"""

from app.imagery.enums import RecordStatus

ALLOWED_RECORD_TRANSITIONS: dict[RecordStatus, frozenset[RecordStatus]] = {
    RecordStatus.UPLOADING: frozenset({RecordStatus.VALIDATING, RecordStatus.FAILED}),
    RecordStatus.VALIDATING: frozenset(
        {RecordStatus.PROCESSING, RecordStatus.NEEDS_INPUT, RecordStatus.FAILED}
    ),
    RecordStatus.PROCESSING: frozenset(
        {RecordStatus.READY, RecordStatus.FAILED, RecordStatus.NEEDS_INPUT}
    ),
    RecordStatus.NEEDS_INPUT: frozenset({RecordStatus.PROCESSING, RecordStatus.FAILED}),
    RecordStatus.READY: frozenset(),
    RecordStatus.FAILED: frozenset(),
}


def is_record_transition_allowed(current: RecordStatus, target: RecordStatus) -> bool:
    return target in ALLOWED_RECORD_TRANSITIONS[current]
