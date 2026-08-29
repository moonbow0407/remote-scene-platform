"""鉴权与用户管理 API 模型。响应绝不包含 password 或 password_hash。"""

import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.context import ActorRole

_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_username(value: str) -> str:
    username = value.strip()
    if not _USERNAME_PATTERN.fullmatch(username):
        raise ValueError(
            "username 须为 3–64 字符，以字母或数字开头，仅含字母、数字、点、下划线与连字符"
        )
    return username


def _validate_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 255 or not _EMAIL_PATTERN.fullmatch(email):
        raise ValueError("email 不是合法邮箱地址")
    return email


def _validate_password(value: str) -> str:
    if len(value) < 8 or len(value) > 128:
        raise ValueError("password 长度须为 8–128 字符")
    return value


class LoginRequest(BaseModel):
    """登录标识可以是 username 或 email。"""

    username: str = Field(min_length=1, max_length=255, description="用户名或邮箱")
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, description="Refresh Token")


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    username: str
    email: str
    role: ActorRole
    is_active: bool
    created_at: datetime
    updated_at: datetime


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    role: ActorRole = ActorRole.USER

    @field_validator("username")
    @classmethod
    def _username(cls, value: str) -> str:
        return _validate_username(value)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _validate_email(value)

    @field_validator("password")
    @classmethod
    def _password(cls, value: str) -> str:
        return _validate_password(value)


class UserUpdateRequest(BaseModel):
    username: str | None = Field(default=None, min_length=3, max_length=64)
    email: str | None = Field(default=None, min_length=3, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    role: ActorRole | None = None

    @field_validator("username")
    @classmethod
    def _username(cls, value: str | None) -> str | None:
        return None if value is None else _validate_username(value)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return None if value is None else _validate_email(value)

    @field_validator("password")
    @classmethod
    def _password(cls, value: str | None) -> str | None:
        return None if value is None else _validate_password(value)

    @model_validator(mode="after")
    def _at_least_one_field(self) -> "UserUpdateRequest":
        if (
            self.username is None
            and self.email is None
            and self.password is None
            and self.role is None
        ):
            raise ValueError("至少提供一个待更新字段")
        return self


class UserStatusRequest(BaseModel):
    is_active: bool
