"""登录和用户接口的请求和响应。响应里不会出现密码。"""

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
    """用用户名或邮箱登录。"""

    model_config = ConfigDict(title="登录")

    username: str = Field(min_length=1, max_length=255, description="用户名或邮箱")
    password: str = Field(min_length=1, max_length=128, description="登录密码")


class RefreshRequest(BaseModel):
    """用刷新令牌换一套新令牌。"""

    model_config = ConfigDict(title="刷新令牌")

    refresh_token: str = Field(min_length=1, description="登录时返回的刷新令牌")


class TokenResponse(BaseModel):
    """登录或刷新成功后的令牌。"""

    model_config = ConfigDict(title="令牌")

    access_token: str = Field(
        description="访问令牌。调用需要登录的接口时放在请求头 Authorization: Bearer <token>"
    )
    refresh_token: str = Field(description="刷新令牌。访问令牌过期后，用它换新的一对令牌")
    token_type: str = Field(default="Bearer", description="令牌类型，固定为 Bearer")
    expires_in: int = Field(description="访问令牌有效时间，单位秒")


class UserPublic(BaseModel):
    """用户资料，不含密码。"""

    model_config = ConfigDict(title="用户", from_attributes=True)

    id: int = Field(description="用户编号")
    username: str = Field(description="登录用户名")
    email: str = Field(description="邮箱")
    role: ActorRole = Field(description="角色")
    is_active: bool = Field(description="是否允许登录。false 表示已停用")
    created_at: datetime = Field(description="创建时间，UTC 且带时区")
    updated_at: datetime = Field(description="最近一次修改时间，UTC 且带时区")


class UserCreateRequest(BaseModel):
    """管理员新建账号。没有自助注册。"""

    model_config = ConfigDict(title="创建用户")

    username: str = Field(
        min_length=3,
        max_length=64,
        description="登录用户名，3–64 个字符，字母或数字开头",
    )
    email: str = Field(min_length=3, max_length=255, description="邮箱，全局不能重复")
    password: str = Field(min_length=8, max_length=128, description="初始密码，8–128 个字符")
    role: ActorRole = Field(default=ActorRole.USER, description="角色，默认普通用户")

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
    """管理员改资料。没写的字段保持原值，至少改一项。"""

    model_config = ConfigDict(title="更新用户")

    username: str | None = Field(
        default=None, min_length=3, max_length=64, description="新用户名；不传则不改"
    )
    email: str | None = Field(
        default=None, min_length=3, max_length=255, description="新邮箱；不传则不改"
    )
    password: str | None = Field(
        default=None, min_length=8, max_length=128, description="新密码；不传则不改"
    )
    role: ActorRole | None = Field(default=None, description="新角色；不传则不改")

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
    """启用或停用账号。"""

    model_config = ConfigDict(title="启停用户")

    is_active: bool = Field(description="true 启用，false 立即禁止该用户登录")
