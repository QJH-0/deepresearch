"""认证路由：登录换取 JWT、查询当前身份。"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import (
    AuthConfigError,
    User,
    authenticate,
    create_access_token,
    get_current_user,
)
from backend.schemas.auth import LoginRequest, TokenResponse, UserInfo

logger = logging.getLogger("backend.auth.router")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest) -> TokenResponse:
    """用用户名口令换取访问令牌。

    失败一律返回 401 且不区分「用户不存在」与「口令错误」；
    JWT_SECRET 未配置时返回 503（服务端问题，不降级为放行）。
    """
    user = authenticate(payload.user_id, payload.password)
    if user is None:
        logger.warning("[auth] 登录失败 | user=%s", payload.user_id)
        raise HTTPException(status_code=401, detail="用户名或口令错误")

    try:
        token, expires_in = create_access_token(user)
    except AuthConfigError as exc:
        logger.error("[auth] 无法签发令牌: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc))

    logger.info("[auth] 登录成功 | user=%s | role=%s", user.user_id, user.role)
    return TokenResponse(
        access_token=token,
        expires_in=expires_in,
        user_id=user.user_id,
        role=user.role,
    )


@router.get("/me", response_model=UserInfo)
async def me(user: User = Depends(get_current_user)) -> UserInfo:
    """返回当前令牌对应的身份（前端用于校验登录态是否仍有效）。"""
    return UserInfo(user_id=user.user_id, role=user.role)
