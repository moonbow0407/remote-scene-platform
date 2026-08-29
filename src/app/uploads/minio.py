"""MinIO 适配器：对象存储操作的唯一入口，便于集成测试替换。

覆盖：Multipart 会话、预签名 URL、分片列举/完成/中止、服务端拷贝、
流式下载与对象删除。文件字节一律不经过 API 进程（预签名直传）。
"""

import hashlib
import logging
from pathlib import Path
from typing import Any, BinaryIO

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError

from app.errors import ProblemError
from app.settings import Settings

logger = logging.getLogger(__name__)


class MinioError(ProblemError):
    """对象存储操作失败（视为瞬时错误，可重试）。"""

    def __init__(self, detail: str) -> None:
        super().__init__(status=503, code="MINIO_ERROR", title="对象存储操作失败", detail=detail)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, BotoCoreError):
        return True
    code = (
        getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if isinstance(exc, ClientError)
        else ""
    )
    # 5xx 类与限流视为瞬时；4xx 语义错误（NoSuchUpload 等）由调用方决定
    return str(code).startswith("5") or code in (
        "SlowDown",
        "RequestTimeout",
        "ThrottlingException",
    )


def get_minio_client(settings: Settings) -> Any:
    scheme = "https" if settings.minio_secure else "http"
    return boto3.client(
        "s3",
        endpoint_url=f"{scheme}://{settings.minio_endpoint}",
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        region_name="us-east-1",
        config=BotoConfig(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=120,
        ),
    )


def get_presign_client(settings: Settings) -> Any:
    """预签名专用客户端：端点必须与浏览器实际访问地址一致（SigV4 签名覆盖主机名）。"""
    from app.settings import minio_public_endpoint

    scheme = "https" if settings.minio_secure else "http"
    return boto3.client(
        "s3",
        endpoint_url=f"{scheme}://{minio_public_endpoint(settings)}",
        aws_access_key_id=settings.minio_access_key,
        aws_secret_access_key=settings.minio_secret_key,
        region_name="us-east-1",
        config=BotoConfig(signature_version="s3v4"),
    )


class MinioAdapter:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = get_minio_client(settings)
        # 预签名 URL 面向浏览器：签名覆盖主机名，必须用外部可达端点单独生成
        self._presign_client = get_presign_client(settings)

    @property
    def bucket(self) -> str:
        return self._settings.minio_bucket

    def create_multipart_upload(self, *, key: str, content_type: str | None) -> str:
        try:
            params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
            if content_type:
                params["ContentType"] = content_type
            response = self._client.create_multipart_upload(**params)
            return str(response["UploadId"])
        except (BotoCoreError, ClientError) as exc:
            raise MinioError(f"创建分片上传会话失败：{exc}") from exc

    def presign_part_url(
        self, *, key: str, upload_id: str, part_number: int, expires_in: int
    ) -> str:
        try:
            return str(
                self._presign_client.generate_presigned_url(
                    "upload_part",
                    Params={
                        "Bucket": self.bucket,
                        "Key": key,
                        "UploadId": upload_id,
                        "PartNumber": part_number,
                    },
                    ExpiresIn=expires_in,
                )
            )
        except (BotoCoreError, ClientError) as exc:
            raise MinioError(f"生成分片预签名 URL 失败：{exc}") from exc

    def presign_get_url(self, *, key: str, expires_in: int) -> str:
        try:
            return str(
                self._presign_client.generate_presigned_url(
                    "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires_in
                )
            )
        except (BotoCoreError, ClientError) as exc:
            raise MinioError(f"生成下载预签名 URL 失败：{exc}") from exc

    def list_parts(self, *, key: str, upload_id: str) -> list[dict[str, Any]]:
        """列举已上传分片（断点续传依据）。"""
        parts: list[dict[str, Any]] = []
        try:
            paginator = self._client.get_paginator("list_parts")
            for page in paginator.paginate(Bucket=self.bucket, Key=key, UploadId=upload_id):
                for part in page.get("Parts", []):
                    parts.append(
                        {
                            "part_number": int(part["PartNumber"]),
                            "etag": str(part["ETag"]).strip('"'),
                            "size": int(part["Size"]),
                        }
                    )
            return parts
        except ClientError as exc:
            if _is_missing_bucket(exc):
                raise MinioError(f"存储桶不存在或不可访问：{self.bucket}") from exc
            if _not_found(exc):
                # 会话已不存在（被中止/清理）视同无分片
                return []
            raise MinioError(f"列举分片失败：{exc}") from exc
        except BotoCoreError as exc:
            raise MinioError(f"列举分片失败：{exc}") from exc

    def complete_multipart_upload(
        self, *, key: str, upload_id: str, parts: list[dict[str, Any]]
    ) -> None:
        try:
            self._client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={
                    "Parts": [
                        {"ETag": p["etag"], "PartNumber": p["part_number"]}
                        for p in sorted(parts, key=lambda x: x["part_number"])
                    ]
                },
            )
        except ClientError as exc:
            if _is_missing_bucket(exc):
                raise MinioError(f"存储桶不存在或不可访问：{self.bucket}") from exc
            if _not_found(exc):
                # 已完成/已中止的会话再次完成：幂等处理，由调用方校验对象存在
                logger.info("分片上传会话已不存在，视为已完成", extra={"key": key})
                return
            raise MinioError(f"完成分片上传失败：{exc}") from exc
        except BotoCoreError as exc:
            raise MinioError(f"完成分片上传失败：{exc}") from exc

    def abort_multipart_upload(self, *, key: str, upload_id: str) -> None:
        try:
            self._client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
        except ClientError as exc:
            if _is_missing_bucket(exc):
                raise MinioError(f"存储桶不存在或不可访问：{self.bucket}") from exc
            if _not_found(exc):
                return
            raise MinioError(f"中止分片上传失败：{exc}") from exc
        except BotoCoreError as exc:
            raise MinioError(f"中止分片上传失败：{exc}") from exc

    def head_object(self, *, key: str) -> dict[str, Any] | None:
        try:
            response = self._client.head_object(Bucket=self.bucket, Key=key)
            return {
                "size": int(response["ContentLength"]),
                "etag": str(response["ETag"]).strip('"'),
            }
        except ClientError as exc:
            if _is_missing_bucket(exc):
                raise MinioError(f"存储桶不存在或不可访问：{self.bucket}") from exc
            if _not_found(exc):
                return None
            raise MinioError(f"读取对象元数据失败：{exc}") from exc
        except BotoCoreError as exc:
            raise MinioError(f"读取对象元数据失败：{exc}") from exc

    def copy_object(self, *, source_key: str, target_key: str) -> None:
        """服务端拷贝（不经 API/Worker 内存），用于会话对象到内容寻址键的落位。"""
        try:
            self._client.copy_object(
                Bucket=self.bucket,
                Key=target_key,
                CopySource={"Bucket": self.bucket, "Key": source_key},
            )
        except (BotoCoreError, ClientError) as exc:
            raise MinioError(f"服务端拷贝对象失败：{exc}") from exc

    def delete_object(self, *, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise MinioError(f"删除对象失败：{exc}") from exc

    def read_head_bytes(self, *, key: str, length: int) -> bytes:
        """范围读取对象前 N 字节（格式嗅探），不下载完整对象。"""
        try:
            response = self._client.get_object(
                Bucket=self.bucket, Key=key, Range=f"bytes=0-{length - 1}"
            )
            data: bytes = response["Body"].read(length)
            response["Body"].close()
            return data
        except (BotoCoreError, ClientError) as exc:
            raise MinioError(f"读取对象头部失败：{exc}") from exc

    def stream_download(self, *, key: str, chunk_size: int = 1024 * 1024) -> Any:
        """流式下载迭代器：调用方逐块消费，内存占用恒定。"""
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            body = response["Body"]
        except (BotoCoreError, ClientError) as exc:
            raise MinioError(f"下载对象失败：{exc}") from exc

        def _iter() -> Any:
            try:
                while True:
                    chunk = body.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            finally:
                body.close()

        return _iter()

    def upload_file(self, *, local_path: str, key: str, content_type: str | None = None) -> None:
        try:
            extra: dict[str, Any] = {"ContentType": content_type} if content_type else {}
            self._client.upload_file(local_path, self.bucket, key, ExtraArgs=extra or None)
        except (BotoCoreError, ClientError) as exc:
            raise MinioError(f"上传对象失败：{exc}") from exc

    def download_to_file(
        self, *, key: str, local_path: str, expected_sha256: str | None = None
    ) -> int:
        """流式下载到本地文件（不整载入内存）；可选校验 SHA-256。返回字节数。

        先写入 `.partial` 再原子替换，中途失败不会留下被误认为完整的目标文件。
        """
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".partial")
        digest = hashlib.sha256()
        size = 0
        try:
            response = self._client.get_object(Bucket=self.bucket, Key=key)
            body: BinaryIO = response["Body"]
            try:
                with open(tmp, "wb") as f:
                    for chunk in iter(lambda: body.read(1024 * 1024), b""):
                        f.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            finally:
                body.close()
        except (BotoCoreError, ClientError) as exc:
            _unlink_quietly(tmp)
            raise MinioError(f"下载对象失败：{exc}") from exc
        except Exception:
            _unlink_quietly(tmp)
            raise
        if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
            _unlink_quietly(tmp)
            raise MinioError(f"对象内容校验失败：{key} 与记录的 SHA-256 不一致")
        tmp.replace(dest)
        return size


def s3_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


def _is_missing_bucket(exc: ClientError) -> bool:
    return s3_error_code(exc) == "NoSuchBucket"


def _not_found(exc: ClientError) -> bool:
    """对象或分片会话不存在。存储桶缺失不是“文件不存在”，必须单独按基础设施故障处理。"""
    return s3_error_code(exc) in ("NoSuchUpload", "NoSuchKey", "404", "NotFound")


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("残留临时文件删除失败", extra={"path": str(path)})
