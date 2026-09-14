"""R3.4 配置热更新测试用例。

T3.4-01 ~ T3.4-07 覆盖：
- reload 成功原子生效
- 非法配置 422 拒载
- JSON 解析失败 422
- 需重启字段提示
- GET /config 脱敏
- ADMIN_TOKEN 鉴权
- reload 回调失效搜索链
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# 触发 backend.router.__init__ 导入 admin_router 模块
import backend.router.admin_router  # noqa: F401


def _admin_user():
    """管理端点已改为 JWT 管理员依赖，测试直接注入身份对象。"""
    from backend.auth import User

    return User(user_id="admin", role="admin")


class TestReloadSuccess:
    """T3.4-01 reload 成功原子生效。"""

    @pytest.mark.asyncio
    async def test_reload_applies_new_config(self, monkeypatch):
        import backend.config.settings as settings_mod

        original = settings_mod.get_business_settings()

        config_data = json.dumps({"max_iterations": 7})
        monkeypatch.setattr(Path, "read_text", lambda self, encoding=None: config_data)

        mod = sys.modules["backend.router.admin_router"]
        response = await mod.reload_config(admin=_admin_user())

        assert response["reloaded"] is True
        assert "max_iterations" in response["applied_fields"]
        assert settings_mod.get_business_settings().max_iterations == 7

        settings_mod._set_business_settings(original)


class TestReloadValidationReject:
    """T3.4-02 非法配置 422 拒载。"""

    @pytest.mark.asyncio
    async def test_bad_type_rejected(self, monkeypatch):
        import backend.config.settings as settings_mod

        original = settings_mod.get_business_settings()

        config_data = json.dumps({"max_iterations": "abc"})
        monkeypatch.setattr(Path, "read_text", lambda self, encoding=None: config_data)

        from fastapi import HTTPException
        mod = sys.modules["backend.router.admin_router"]

        with pytest.raises(HTTPException) as exc_info:
            await mod.reload_config(admin=_admin_user())
        assert exc_info.value.status_code == 422
        assert settings_mod.get_business_settings().max_iterations == original.max_iterations


class TestReloadJSONParseFail:
    """T3.4-03 JSON 解析失败 422。"""

    @pytest.mark.asyncio
    async def test_bad_json_rejected(self, monkeypatch):
        import backend.config.settings as settings_mod

        original = settings_mod.get_business_settings()

        monkeypatch.setattr(Path, "read_text", lambda self, encoding=None: "{invalid json")

        from fastapi import HTTPException
        mod = sys.modules["backend.router.admin_router"]

        with pytest.raises(HTTPException) as exc_info:
            await mod.reload_config(admin=_admin_user())
        assert exc_info.value.status_code == 422
        assert settings_mod.get_business_settings().max_iterations == original.max_iterations


class TestRestartRequiredFields:
    """T3.4-04 需重启字段提示。"""

    @pytest.mark.asyncio
    async def test_model_change_flags_restart(self, monkeypatch):
        import backend.config.settings as settings_mod

        original = settings_mod.get_business_settings()

        config_data = json.dumps({"model": "qwen-max"})
        monkeypatch.setattr(Path, "read_text", lambda self, encoding=None: config_data)

        mod = sys.modules["backend.router.admin_router"]
        response = await mod.reload_config(admin=_admin_user())

        assert "model" in response["restart_required_fields"]
        assert settings_mod.get_business_settings().model == "qwen-max"

        settings_mod._set_business_settings(original)


class TestGetConfigSanitized:
    """T3.4-05 GET /config 脱敏。"""

    @pytest.mark.asyncio
    async def test_no_sensitive_fields(self):
        mod = sys.modules["backend.router.admin_router"]
        data = await mod.get_config(admin=_admin_user())

        forbidden = {
            "api_key", "dashscope_api_key", "postgres_dsn",
            "redis_url", "rabbitmq_url", "admin_token", "minio_secret_key",
        }
        for field in forbidden:
            assert field not in data, f"敏感字段 {field} 不应出现在 /config 响应中"

        assert "model" in data
        assert "max_iterations" in data


class TestReloadCallbackResetsProviderChain:
    """T3.4-07 reload 回调失效搜索链。"""

    @pytest.mark.asyncio
    async def test_provider_chain_reset(self, monkeypatch):
        import backend.config.settings as settings_mod
        import mult_agents.tools as tools_mod

        original = settings_mod.get_business_settings()
        tools_mod._PROVIDER_CHAIN = MagicMock()
        assert tools_mod._PROVIDER_CHAIN is not None

        config_data = json.dumps({"max_iterations": 9})
        monkeypatch.setattr(Path, "read_text", lambda self, encoding=None: config_data)

        mod = sys.modules["backend.router.admin_router"]
        await mod.reload_config(admin=_admin_user())

        assert tools_mod._PROVIDER_CHAIN is None

        settings_mod._set_business_settings(original)
