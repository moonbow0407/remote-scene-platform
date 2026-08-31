"""鉴权与用户管理 API 模型。响应绝不包含 password 或 password_hash。"""

import re
from datetime import datetime

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
    password: str = Field(min_length=1, max_length=128, description="登录密码")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1, description="刷新令牌，由登录接口返回")


class TokenResponse(BaseModel):
    access_token: str = Field(description="访问令牌，请求受保护接口时放在 Authorization: Bearer")
    refresh_token: str = Field(description="刷新令牌，用于换取新的访问令牌")
    token_type: str = Field(default="Bearer", description="令牌类型，固定 Bearer")
    expires_in: int = Field(description="访问令牌有效期，单位秒")


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int = Field(description="用户 ID")
    username: str = Field(description="登录用户名")
    email: str = Field(description="邮箱")
    role: ActorRole = Field(description="角色：ADMIN 管理员、USER 普通用户")
    is_active: bool = Field(description="是否启用；false 后无法登录")
    created_at: datetime = Field(description="创建时间（UTC，带时区）")
    updated_at: datetime = Field(description="最近更新时间（UTC，带时区）")


class UserCreateRequest(BaseModel):
    username: str = Field(
        min_length=3, max_length=64, description="登录用户名，3–64 字符，字母或数字开头"
    )
    email: str = Field(min_length=3, max_length=255, description="邮箱，全局唯一")
    password: str = Field(min_length=8, max_length=128, description="初始密码，8–128 字符")
    role: ActorRole = Field(default=ActorRole.USER, description="角色：ADMIN 管理员、USER 普通用户")

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
    username: str | None = Field(
        default=None, min_length=3, max_length=64, description="新用户名；省略表示不改"
    )
    email: str | None = Field(
        default=None, min_length=3, max_length=255, description="新邮箱；省略表示不改"
    )
    password: str | None = Field(
        default=None, min_length=8, max_length=128, description="新密码；省略表示不改"
    )
    role: ActorRole | None = Field(default=None, description="新角色；省略表示不改")

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
    is_active: bool = Field(description="是否启用；false 立即禁止该用户登录")
