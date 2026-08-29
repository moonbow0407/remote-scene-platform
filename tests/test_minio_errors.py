"""MinIO 错误分类：存储桶缺失不能当成对象不存在。"""

from botocore.exceptions import ClientError

from app.uploads.minio import _is_missing_bucket, _not_found


def _error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "fixture"}}, "HeadObject")


def test_nosuchbucket_is_infrastructure_not_missing_object() -> None:
    exc = _error("NoSuchBucket")
    assert _is_missing_bucket(exc)
    assert not _not_found(exc)


def test_missing_object_and_upload_are_not_found() -> None:
    assert _not_found(_error("NoSuchKey"))
    assert _not_found(_error("NoSuchUpload"))
    assert _not_found(_error("404"))
    assert not _is_missing_bucket(_error("NoSuchKey"))
