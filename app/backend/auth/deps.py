"""FastAPI 认证依赖。"""

import logging

from fastapi import Depends, Header, HTTPException

from .security import AuthConfigError, User, decode_access_token

logger = logging.getLogger("backend.auth.deps")

_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": "Bearer"}


def _extract_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=401, detail="缺少 Authorization 头", headers=_UNAUTHORIZED_HEADERS
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="Authorization 头格式应为 'Bearer <token>'",
            headers=_UNAUTHORIZED_HEADERS,
        )
    return token.strip()


async def get_current_user(authorization: str | None = Header(default=None)) -> User:
    """从 Bearer 令牌解析当前用户；未认证一律 401。

    业务接口的 user_id 必须取自这里的返回值 —— 不得再从请求参数读取。
    """
    token = _extract_bearer_token(authorization)
    try:
        return decode_access_token(token)
    except AuthConfigError as exc:
        # 服务端配置问题，不是调用方凭证问题
        logger.error("[auth] 认证配置缺失: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))
    except ValueError as exc:
        logger.info("[auth] 令牌校验失败: %s", exc)
        raise HTTPException(status_code=401, detail=str(exc), headers=_UNAUTHORIZED_HEADERS)


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """要求管理员角色。"""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
