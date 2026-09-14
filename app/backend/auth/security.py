"""JWT 签发与校验、口令比对。

用户来源当前是环境变量 AUTH_USERS（"user:pass[:role],user2:pass2"）——
项目没有用户表，先用配置承载；接入真实用户体系时只需替换 authenticate()。
"""

import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt

from backend.config.settings import MiddlewareSettings

logger = logging.getLogger("backend.auth.security")

_DEFAULT_ALGORITHM = "HS256"

# 用户不存在时用它做一次比对，避免用响应时间区分「用户不存在」与「口令错误」
_DUMMY_PASSWORD = "\x00" * 32


@dataclass(frozen=True)
class User:
    """令牌解析出的身份。"""

    user_id: str
    role: str = "user"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthConfigError(RuntimeError):
    """认证配置缺失或非法（fail-closed，不降级为放行）。"""


def parse_users(raw: str) -> dict[str, tuple[str, str]]:
    """解析 AUTH_USERS 配置。

    格式：``user:pass[:role],user2:pass2``；role 省略时视为普通用户。
    条目缺少用户名或口令时跳过（不做静默兜底账号）。
    """
    users: dict[str, tuple[str, str]] = {}
    for item in (raw or "").split(","):
        parts = [part.strip() for part in item.split(":")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        role = parts[2] if len(parts) > 2 and parts[2] else "user"
        users[parts[0]] = (parts[1], role)
    return users


def authenticate(user_id: str, password: str) -> Optional[User]:
    """校验用户名与口令；成功返回 User，失败返回 None。"""
    entry = parse_users(MiddlewareSettings().auth_users).get(user_id or "")
    if entry is None:
        hmac.compare_digest(password or "", _DUMMY_PASSWORD)
        return None

    expected_password, role = entry
    if not hmac.compare_digest(password or "", expected_password):
        return None
    return User(user_id=user_id, role=role)


def _secret() -> str:
    secret = MiddlewareSettings().jwt_secret
    if not secret:
        raise AuthConfigError("JWT_SECRET 未配置，无法签发或校验令牌")
    return secret


def _algorithm() -> str:
    return MiddlewareSettings().jwt_algorithm or _DEFAULT_ALGORITHM


def create_access_token(user: User) -> tuple[str, int]:
    """签发访问令牌，返回 (token, 有效期秒数)。"""
    expire_minutes = max(1, int(MiddlewareSettings().jwt_expire_minutes))
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.user_id,
        "role": user.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expire_minutes)).timestamp()),
    }
    token = jwt.encode(payload, _secret(), algorithm=_algorithm())
    return token, expire_minutes * 60


def decode_access_token(token: str) -> User:
    """校验并解析令牌。

    Raises:
        AuthConfigError: 服务端密钥未配置（应转 503，而不是 401）
        ValueError: 令牌无效或已过期（应转 401）
    """
    try:
        payload = jwt.decode(
            token,
            _secret(),
            algorithms=[_algorithm()],
            options={"require": ["exp", "sub"]},
        )
    except AuthConfigError:
        raise
    except jwt.PyJWTError as exc:
        raise ValueError(f"令牌无效或已过期: {exc}") from exc

    user_id = str(payload.get("sub") or "").strip()
    if not user_id:
        raise ValueError("令牌缺少 sub")
    return User(user_id=user_id, role=str(payload.get("role") or "user"))
