"""JWT 认证测试。

覆盖令牌签发/校验、口令比对、FastAPI 依赖行为，以及两条结构性守卫：
1. 所有业务路由都必须挂认证依赖（防止新增端点漏挂）
2. 请求模型里不得再出现 user_id（身份只能来自令牌）
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from backend.auth import (
    AuthConfigError,
    User,
    authenticate,
    create_access_token,
    decode_access_token,
    parse_users,
)

# 长度需 >= 32 字节，否则 PyJWT 会告警（HS256 的推荐下限）
_SECRET = "unit-test-secret-0123456789abcdef"
_USERS = "alice:alicepw,root:rootpw:admin"


@pytest.fixture
def auth_settings(monkeypatch):
    """把认证配置固定为测试值。"""
    import backend.auth.security as security

    monkeypatch.setattr(
        security,
        "MiddlewareSettings",
        lambda: SimpleNamespace(
            auth_users=_USERS,
            jwt_secret=_SECRET,
            jwt_algorithm="HS256",
            jwt_expire_minutes=30,
        ),
    )
    return security


# ──────────────────────────────────────────────────────────────
# 用户配置解析与口令比对
# ──────────────────────────────────────────────────────────────


class TestParseUsers:
    def test_parses_role_and_defaults_to_user(self):
        users = parse_users("alice:pw,root:pw2:admin")

        assert users["alice"] == ("pw", "user")
        assert users["root"] == ("pw2", "admin")

    @pytest.mark.parametrize("raw", ["", "   ", "nopassword", ":pw", "user:", "a:b,,c:d"])
    def test_skips_malformed_entries(self, raw):
        users = parse_users(raw)

        assert "nopassword" not in users
        assert "" not in users
        if raw == "a:b,,c:d":
            assert users == {"a": ("b", "user"), "c": ("d", "user")}


class TestAuthenticate:
    def test_success_returns_user_with_role(self, auth_settings):
        user = authenticate("root", "rootpw")

        assert user is not None
        assert user.user_id == "root"
        assert user.is_admin is True

    def test_normal_user_is_not_admin(self, auth_settings):
        assert authenticate("alice", "alicepw").is_admin is False

    def test_wrong_password_returns_none(self, auth_settings):
        assert authenticate("alice", "wrong") is None

    def test_unknown_user_returns_none(self, auth_settings):
        assert authenticate("nobody", "whatever") is None

    def test_empty_auth_users_denies_everyone(self, auth_settings, monkeypatch):
        monkeypatch.setattr(
            auth_settings, "MiddlewareSettings",
            lambda: SimpleNamespace(
                auth_users="", jwt_secret=_SECRET, jwt_algorithm="HS256", jwt_expire_minutes=30
            ),
        )

        assert authenticate("alice", "alicepw") is None


# ──────────────────────────────────────────────────────────────
# 令牌签发与校验
# ──────────────────────────────────────────────────────────────


class TestAccessToken:
    def test_round_trip_preserves_identity_and_role(self, auth_settings):
        token, expires_in = create_access_token(User("root", "admin"))

        decoded = decode_access_token(token)

        assert decoded.user_id == "root"
        assert decoded.role == "admin"
        assert expires_in == 30 * 60

    def test_expired_token_rejected(self, auth_settings):
        expired = pyjwt.encode(
            {"sub": "alice", "role": "user", "exp": int(time.time()) - 10},
            _SECRET,
            algorithm="HS256",
        )

        with pytest.raises(ValueError, match="令牌无效或已过期"):
            decode_access_token(expired)

    def test_token_signed_with_other_secret_rejected(self, auth_settings):
        forged = pyjwt.encode(
            {"sub": "alice", "exp": int(time.time()) + 60}, "other-secret-0123456789abcdefghij", algorithm="HS256"
        )

        with pytest.raises(ValueError):
            decode_access_token(forged)

    def test_token_without_sub_rejected(self, auth_settings):
        no_sub = pyjwt.encode({"exp": int(time.time()) + 60}, _SECRET, algorithm="HS256")

        with pytest.raises(ValueError):
            decode_access_token(no_sub)

    def test_missing_secret_fails_closed(self, auth_settings, monkeypatch):
        """JWT_SECRET 未配置时必须拒绝，而不是放行。"""
        monkeypatch.setattr(
            auth_settings, "MiddlewareSettings",
            lambda: SimpleNamespace(
                auth_users=_USERS, jwt_secret="", jwt_algorithm="HS256", jwt_expire_minutes=30
            ),
        )

        with pytest.raises(AuthConfigError):
            create_access_token(User("alice"))
        with pytest.raises(AuthConfigError):
            decode_access_token("anything")


# ──────────────────────────────────────────────────────────────
# FastAPI 依赖
# ──────────────────────────────────────────────────────────────


class TestAuthDependencies:
    async def _call(self, authorization):
        from backend.auth import get_current_user

        return await get_current_user(authorization=authorization)

    async def test_missing_header_is_401(self):
        with pytest.raises(HTTPException) as exc:
            await self._call(None)

        assert exc.value.status_code == 401

    @pytest.mark.parametrize("header", ["Token abc", "Bearer", "Bearer   ", "abc"])
    async def test_malformed_header_is_401(self, header):
        with pytest.raises(HTTPException) as exc:
            await self._call(header)

        assert exc.value.status_code == 401

    async def test_invalid_token_is_401(self, auth_settings):
        with pytest.raises(HTTPException) as exc:
            await self._call("Bearer not-a-jwt")

        assert exc.value.status_code == 401

    async def test_valid_token_returns_user(self, auth_settings):
        token, _ = create_access_token(User("alice"))

        user = await self._call(f"Bearer {token}")

        assert user.user_id == "alice"

    async def test_missing_secret_is_503_not_401(self, auth_settings, monkeypatch):
        """服务端配置缺失应与调用方凭证错误区分开。"""
        monkeypatch.setattr(
            auth_settings, "MiddlewareSettings",
            lambda: SimpleNamespace(
                auth_users=_USERS, jwt_secret="", jwt_algorithm="HS256", jwt_expire_minutes=30
            ),
        )

        with pytest.raises(HTTPException) as exc:
            await self._call("Bearer whatever")

        assert exc.value.status_code == 503

    async def test_require_admin_rejects_normal_user(self):
        from backend.auth import require_admin

        with pytest.raises(HTTPException) as exc:
            await require_admin(User("alice", "user"))

        assert exc.value.status_code == 403

    async def test_require_admin_allows_admin(self):
        from backend.auth import require_admin

        assert (await require_admin(User("root", "admin"))).is_admin is True


# ──────────────────────────────────────────────────────────────
# 登录端点
# ──────────────────────────────────────────────────────────────


class TestLoginRoute:
    def _module(self):
        from importlib import import_module

        return import_module("backend.router.auth_router")

    async def test_login_success_returns_token(self, auth_settings):
        from backend.schemas.auth import LoginRequest

        resp = await self._module().login(LoginRequest(user_id="alice", password="alicepw"))

        assert resp.token_type == "bearer"
        assert resp.user_id == "alice"
        assert resp.expires_in > 0
        assert decode_access_token(resp.access_token).user_id == "alice"

    async def test_login_failure_is_401_without_enumeration(self, auth_settings):
        from backend.schemas.auth import LoginRequest

        with pytest.raises(HTTPException) as exc:
            await self._module().login(LoginRequest(user_id="alice", password="wrong"))

        assert exc.value.status_code == 401
        # 不区分「用户不存在」与「口令错误」，避免账号枚举
        with pytest.raises(HTTPException) as exc2:
            await self._module().login(LoginRequest(user_id="nobody", password="wrong"))

        assert exc2.value.detail == exc.value.detail

    async def test_me_returns_identity(self):
        from backend.auth import User as AuthUser

        info = await self._module().me(AuthUser("alice", "user"))

        assert info.user_id == "alice"
        assert info.role == "user"


# ──────────────────────────────────────────────────────────────
# 结构性守卫
# ──────────────────────────────────────────────────────────────


def _requires_auth(route: APIRoute) -> bool:
    from backend.auth import get_current_user, require_admin

    def _walk(dep) -> bool:
        if dep.call in (get_current_user, require_admin):
            return True
        return any(_walk(child) for child in dep.dependencies)

    return any(_walk(dep) for dep in route.dependant.dependencies)


class TestRouteAuthCoverage:
    """守卫：新增业务端点时如果忘了挂认证依赖，这里会失败。"""

    PUBLIC_PATHS = {"/health", "/health/live", "/api/v1/auth/login"}

    def test_every_business_route_requires_authentication(self):
        import app_main

        unprotected = []
        for route in app_main.app.routes:
            if not isinstance(route, APIRoute):
                continue
            if not route.path.startswith("/api/"):
                continue
            if route.path in self.PUBLIC_PATHS:
                continue
            if not _requires_auth(route):
                unprotected.append(f"{sorted(route.methods)} {route.path}")

        assert not unprotected, f"以下端点未要求认证: {unprotected}"

    def test_request_models_do_not_accept_user_id(self):
        """身份只能来自令牌；请求体里出现 user_id 就等于允许调用方冒充他人。"""
        from backend.schemas.document import DocumentBatchDeleteRequest
        from backend.schemas.research import (
            ResearchRequest,
            ThreadPinRequest,
            ThreadRenameRequest,
        )

        for model in (ResearchRequest, ThreadRenameRequest, ThreadPinRequest, DocumentBatchDeleteRequest):
            assert "user_id" not in model.model_fields, (
                f"{model.__name__} 不应接受 user_id"
            )

    def test_auth_router_is_registered(self):
        import app_main

        paths = {route.path for route in app_main.app.routes if isinstance(route, APIRoute)}

        assert "/api/v1/auth/login" in paths
        assert "/api/v1/auth/me" in paths
