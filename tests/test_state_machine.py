"""Job 与资产版本状态机转换规则。"""

from app.assets.enums import AssetVersionStatus
from app.assets.version_state import is_version_transition_allowed
from app.jobs.enums import JobStatus
from app.jobs.state_machine import is_transition_allowed


def test_job_happy_path() -> None:
    assert is_transition_allowed(JobStatus.PENDING, JobStatus.QUEUED)
    assert is_transition_allowed(JobStatus.QUEUED, JobStatus.RUNNING)
    assert is_transition_allowed(JobStatus.RUNNING, JobStatus.RETRYING)
    assert is_transition_allowed(JobStatus.RETRYING, JobStatus.QUEUED)
    assert is_transition_allowed(JobStatus.QUEUED, JobStatus.RUNNING)
    assert is_transition_allowed(JobStatus.RUNNING, JobStatus.SUCCEEDED)


def test_job_needs_input_resume_path() -> None:
    assert is_transition_allowed(JobStatus.RUNNING, JobStatus.NEEDS_INPUT)
    assert is_transition_allowed(JobStatus.NEEDS_INPUT, JobStatus.QUEUED)
    assert is_transition_allowed(JobStatus.QUEUED, JobStatus.RUNNING)
    assert is_transition_allowed(JobStatus.RUNNING, JobStatus.SUCCEEDED)


def test_job_terminal_states_have_no_outgoing() -> None:
    for status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.MISSED):
        for target in JobStatus:
            assert not is_transition_allowed(status, target)


def test_job_invalid_jump_rejected() -> None:
    # Broker 消息可能先于 Dispatcher 的 QUEUED 回写到达，允许 Worker 直接认领。
    assert is_transition_allowed(JobStatus.PENDING, JobStatus.RUNNING)
    assert not is_transition_allowed(JobStatus.QUEUED, JobStatus.SUCCEEDED)
    assert not is_transition_allowed(JobStatus.NEEDS_INPUT, JobStatus.SUCCEEDED)


def test_version_processing_path() -> None:
    assert is_version_transition_allowed(
        AssetVersionStatus.VALIDATING, AssetVersionStatus.PROCESSING
    )
    assert is_version_transition_allowed(AssetVersionStatus.PROCESSING, AssetVersionStatus.READY)
    assert is_version_transition_allowed(
        AssetVersionStatus.PROCESSING, AssetVersionStatus.NEEDS_INPUT
    )
    assert is_version_transition_allowed(
        AssetVersionStatus.NEEDS_INPUT, AssetVersionStatus.PROCESSING
    )


def test_version_invalid_jump_rejected() -> None:
    assert not is_version_transition_allowed(
        AssetVersionStatus.VALIDATING, AssetVersionStatus.READY
    )
    assert not is_version_transition_allowed(
        AssetVersionStatus.READY, AssetVersionStatus.PROCESSING
    )
    assert not is_version_transition_allowed(
        AssetVersionStatus.FAILED, AssetVersionStatus.PROCESSING
    )
