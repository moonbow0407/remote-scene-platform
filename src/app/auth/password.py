"""Argon2id 密码哈希。不自行实现算法，不在日志中输出口令。"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# PasswordHasher 默认 type=Type.ID（Argon2id）
_HASHER = PasswordHasher()
_DUMMY_HASH: str | None = None


def hash_password(password: str) -> str:
    """返回 Argon2id 哈希；明文不得持久化。"""
    return _HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """校验明文是否匹配哈希；哈希损坏或算法不匹配视为失败，不抛给调用方。"""
    try:
        return _HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_password_hash() -> str:
    """用于登录时对不存在用户做等时校验，避免按耗时枚举账号。"""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("timing-equalization-dummy")
    return _DUMMY_HASH
