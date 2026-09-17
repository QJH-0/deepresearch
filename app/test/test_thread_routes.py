"""T2.2-10 后端 state/messages 只读路由测试。

验证 GET /api/v1/research/threads/{id}/state 与 GET /api/v1/research/threads/{id}/messages
路由存在且返回正确字段结构。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_thread_routes.py -v
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))


def _test_user():
    """路由已要求认证，测试直接注入身份对象（鉴权行为由 test_auth.py 覆盖）。"""
    from backend.auth import User

    return User(user_id="default_user", role="user")


def _make_snapshot(*, next_nodes=(), interrupts=(), values=None, parent_config=None):
    """构造 mock graph snapshot。"""
    snap = MagicMock()
    snap.next = next_nodes
    snap.interrupts = interrupts
    snap.values = values or {}
    snap.parent_config = parent_config
    snap.created_at = "2026-09-06T12:00:00Z"
    snap.config = {"configurable": {"thread_id": "test-thread"}}
    return snap


def _make_research_service_mock():
    """构造 mock ResearchService，get_state / get_thread_messages 已 stub。"""
    svc = MagicMock()
    svc._ensure_initialized = MagicMock()
    svc.get_state = AsyncMock(return_value={
        "thread_id": "test-thread",
        "status": "idle",
        "current_node": "",
        "has_checkpoint": True,
        "resumable": True,
        "interrupted_by_restart": False,
        "next_nodes": ["write"],
        "values": {"query": "测试", "final": ""},
        "interrupts": [],
        "created_at": "2026-09-06T12:00:00Z",
        "parent_config": None,
    })
    svc.get_thread_messages = AsyncMock(return_value=[
        {"role": "user", "content": "测试问题"},
        {"role": "assistant", "content": "测试回答"},
    ])
    return svc


@pytest.fixture
def research_service():
    return _make_research_service_mock()


@pytest.fixture
def task_registry():
    """mock TaskRegistry：is_running 返回 False，redis 为 None。"""
    tr = MagicMock()
    tr.is_running = MagicMock(return_value=False)
    tr.redis = None
    tr.is_interrupted_by_restart = AsyncMock(return_value=False)
    return tr


# ──────────────────────────────────────────────
# T2.2-10a: GET /threads/{id}/state
# ──────────────────────────────────────────────


class TestGetThreadState:
    """验证 state 只读路由返回结构。"""

    @pytest.mark.asyncio
    async def test_state_returns_required_fields(self, research_service):
        from backend.router.research_router import get_thread_state

        state = await get_thread_state("test-thread", _test_user(), research_service)

        assert "status" in state
        assert "resumable" in state
        assert "interrupted_by_restart" in state
        assert state["thread_id"] == "test-thread"
        assert state["status"] == "idle"
        assert state["resumable"] is True

    @pytest.mark.asyncio
    async def test_state_awaiting_input(self, research_service):
        from backend.router.research_router import get_thread_state

        research_service.get_state = AsyncMock(return_value={
            "thread_id": "test-thread",
            "status": "awaiting_input",
            "resumable": True,
            "interrupted_by_restart": False,
            "next_nodes": [],
            "values": {},
            "interrupts": [],
        })

        state = await get_thread_state("test-thread", _test_user(), research_service)

        assert state["status"] == "awaiting_input"
        assert state["resumable"] is True


# ──────────────────────────────────────────────
# T2.2-10b: GET /threads/{id}/messages
# ──────────────────────────────────────────────


class TestGetThreadMessages:
    """验证 messages 只读路由返回结构。"""

    @pytest.mark.asyncio
    async def test_messages_returns_list(self, research_service):
        from backend.router.research_router import get_thread_messages

        result = await get_thread_messages("test-thread", _test_user(), research_service)

        assert result["thread_id"] == "test-thread"
        assert isinstance(result["messages"], list)
        assert len(result["messages"]) == 2
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_messages_empty_thread(self, research_service):
        from backend.router.research_router import get_thread_messages

        research_service.get_thread_messages = AsyncMock(return_value=[])

        result = await get_thread_messages("empty-thread", _test_user(), research_service)

        assert result["thread_id"] == "empty-thread"
        assert result["messages"] == []


# ──────────────────────────────────────────────
# T2.2-10c: get_state（service 层）返回字段完整性
# ──────────────────────────────────────────────


class TestGetStateService:
    """直接测 service 层 get_state 返回字段，不依赖路由。"""

    @pytest.mark.asyncio
    async def test_state_has_all_required_keys(self, research_service):
        """get_state 返回 dict 包含 status / resumable / interrupted_by_restart。"""
        state = await research_service.get_state("test-thread")

        required_keys = {"status", "resumable", "interrupted_by_restart", "thread_id"}
        assert required_keys.issubset(set(state.keys()))


# ──────────────────────────────────────────────
# 会话归属守卫：thread_id 由调用方提供，必须校验归属
# ──────────────────────────────────────────────


def _svc_with_owner(owner):
    """构造仅暴露 get_thread_owner 的 ResearchService 替身。"""
    svc = MagicMock()
    svc.get_thread_owner = MagicMock(return_value=owner)
    return svc


class TestThreadAccessGuard:
    """守卫语义：非本人会话一律 404，不泄露资源存在性。"""

    @pytest.mark.asyncio
    async def test_owned_thread_passes(self):
        from backend.router.research_router import require_thread_access

        user = _test_user()
        got = await require_thread_access("t1", user, _svc_with_owner("default_user"))

        assert got is user

    @pytest.mark.asyncio
    async def test_other_users_thread_returns_404(self):
        from backend.router.research_router import require_thread_access

        with pytest.raises(HTTPException) as exc:
            await require_thread_access("t1", _test_user(), _svc_with_owner("someone_else"))

        # 必须 404 而非 403 —— 403 等于承认该 thread_id 存在，可用于枚举他人会话
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_missing_record_returns_404_for_read_routes(self):
        from backend.router.research_router import require_thread_access

        with pytest.raises(HTTPException) as exc:
            await require_thread_access("t1", _test_user(), _svc_with_owner(None))

        assert exc.value.status_code == 404

    def test_missing_record_is_allowed_for_create_routes(self):
        from backend.router.research_router import _assert_thread_access

        # allow_missing=True 表示「尚无归属记录 = 新会话」，必须放行
        _assert_thread_access(_svc_with_owner(None), "t1", _test_user(), allow_missing=True)

    def test_other_users_thread_rejected_even_when_missing_allowed(self):
        from backend.router.research_router import _assert_thread_access

        with pytest.raises(HTTPException) as exc:
            _assert_thread_access(
                _svc_with_owner("someone_else"), "t1", _test_user(), allow_missing=True
            )

        assert exc.value.status_code == 404


class TestBodyThreadRoutesRejectForeignThread:
    """请求体携带 thread_id 的路由无法用依赖注入守卫，需逐个断言拒绝。"""

    @pytest.mark.asyncio
    async def test_run_rejects_foreign_thread(self):
        from backend.router.research_router import run_research
        from backend.schemas import ResearchRequest

        payload = ResearchRequest(query="q", thread_id="t1", tenant_id="default_tenant")
        svc = _svc_with_owner("someone_else")
        svc.run = AsyncMock(return_value="")

        with pytest.raises(HTTPException) as exc:
            await run_research(payload, _test_user(), svc)

        assert exc.value.status_code == 404
        svc.run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_rejects_foreign_thread(self, task_registry):
        from backend.router.research_router import cancel_research, CancelRequest

        with pytest.raises(HTTPException) as exc:
            await cancel_research(
                CancelRequest(thread_id="t1"), _test_user(), _svc_with_owner("someone_else")
            )

        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_rollback_rejects_foreign_thread(self):
        from backend.router.research_router import rollback
        from backend.schemas import RollbackRequest

        svc = _svc_with_owner("someone_else")
        svc.update_state = AsyncMock()

        with pytest.raises(HTTPException) as exc:
            await rollback(RollbackRequest(thread_id="t1", values={}), _test_user(), svc)

        assert exc.value.status_code == 404
        svc.update_state.assert_not_awaited()


class TestThreadScopedRoutesAreGuarded:
    """结构性回归：新增 thread_id 作用域路由时不得漏挂守卫。"""

    # 请求体携带 thread_id、无法用路径依赖守卫的路由，守卫在函数体内调用
    _BODY_THREAD_ROUTES = {
        ("POST", "/api/v1/research/run"),
        ("POST", "/api/v1/research/stream"),
        ("POST", "/api/v1/research/resume"),
        ("POST", "/api/v1/research/cancel"),
        ("POST", "/api/v1/research/rollback"),
    }

    # 按 user_id 过滤、不涉及具体 thread_id 的接口，无需守卫
    _USER_SCOPED_ROUTES = {
        ("GET", "/api/v1/research/threads"),
        ("GET", "/api/v1/research/memories"),
    }

    @staticmethod
    def _dependency_names(route) -> set:
        names, stack = set(), [route.dependant]
        while stack:
            dep = stack.pop()
            if dep.call is not None:
                names.add(getattr(dep.call, "__name__", ""))
            stack.extend(dep.dependencies)
        return names

    def _thread_scoped_path_routes(self):
        from backend.router import research_router

        return [
            route for route in research_router.routes
            if "{thread_id}" in getattr(route, "path", "")
        ]

    def test_every_path_thread_route_declares_guard(self):
        unguarded = [
            route.path for route in self._thread_scoped_path_routes()
            if "require_thread_access" not in self._dependency_names(route)
        ]

        assert unguarded == [], f"以下路由缺少会话归属守卫: {unguarded}"

    def test_body_thread_routes_have_no_path_guard(self):
        """反向断言：未挂依赖的路由必须正好是已知的请求体路由 + 用户作用域路由集合。

        这样将来新增携带 thread_id 的路由时，本用例会失败并提示补守卫。
        """
        from backend.router import research_router

        without_guard = {
            (method, route.path)
            for route in research_router.routes
            if "require_thread_access" not in self._dependency_names(route)
            for method in (getattr(route, "methods", None) or [])
        }

        assert without_guard == self._BODY_THREAD_ROUTES | self._USER_SCOPED_ROUTES
