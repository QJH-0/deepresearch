"""认证相关请求/响应模型。"""

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    user_id: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # 秒
    user_id: str
    role: str


class UserInfo(BaseModel):
    user_id: str
    role: str
