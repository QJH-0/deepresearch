"""认证模块：JWT 签发校验与 FastAPI 依赖。

设计要点：
1. **身份只来自令牌** —— 业务接口的 user_id 一律由令牌的 sub 派生，
   不再从请求参数读取（历史实现允许调用方自由指定 user_id，构成越权访问）。
2. **fail-closed** —— 未配置 JWT_SECRET 时拒绝签发与校验（503），
   而不是放行；用户名口令错误一律 401，不区分「用户不存在」与「口令错误」。
3. 用户来源为环境变量 AUTH_USERS（"user:pass[:role],..."），
   项目当前没有用户表，先用配置承载；接入真实用户体系时只需替换 authenticate()。
"""

from .deps import get_current_user, require_admin
from .security import (
    AuthConfigError,
    User,
    authenticate,
    create_access_token,
    decode_access_token,
    parse_users,
)

__all__ = [
    "AuthConfigError",
    "User",
    "authenticate",
    "create_access_token",
    "decode_access_token",
    "get_current_user",
    "parse_users",
    "require_admin",
]
