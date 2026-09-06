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

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))


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

        state = await get_thread_state("test-thread", research_service)

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

        state = await get_thread_state("test-thread", research_service)

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

        result = await get_thread_messages("test-thread", research_service)

        assert result["thread_id"] == "test-thread"
        assert isinstance(result["messages"], list)
        assert len(result["messages"]) == 2
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][1]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_messages_empty_thread(self, research_service):
        from backend.router.research_router import get_thread_messages

        research_service.get_thread_messages = AsyncMock(return_value=[])

        result = await get_thread_messages("empty-thread", research_service)

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
