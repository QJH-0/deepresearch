"""Phase 2 测试：SSE 事件协议、research_service 结构性验证。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    set PYTHONPATH=app
    python -m pytest app/test/test_p2.py -v
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "app"))

from backend.schemas.events import event, sse, EventEnvelope, EVENT_REGISTRY  # noqa: E402


# ──────────────────────────────────────────────
# T2-5 事件协议合规
# ──────────────────────────────────────────────


def test_event_envelope_structure():
    """EventEnvelope 包含 type/ts/data 三字段。"""
    env = event("run.started", thread_id="t1", run_id="r1")
    assert env.type == "run.started"
    assert isinstance(env.ts, int)
    assert env.data["thread_id"] == "t1"
    assert env.data["run_id"] == "r1"


def test_sse_format():
    """sse() 输出格式为 data: {json}\\n\\n。"""
    env = event("message.delta", message_id="m1", text="hello")
    line = sse(env)
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    payload = json.loads(line[6:].strip())
    assert payload["type"] == "message.delta"
    assert payload["data"]["message_id"] == "m1"
    assert payload["data"]["text"] == "hello"


def test_all_event_types_in_registry():
    """所有定义的事件类型都在 EVENT_REGISTRY 中。"""
    expected = {
        "run.started", "agent.status", "message.start", "message.delta",
        "message.thinking", "sources.found", "interrupt.raised",
        "run.completed", "run.cancelled", "run.error",
    }
    assert set(EVENT_REGISTRY.keys()) == expected


def test_event_validation_rejects_unknown_type():
    """未知事件类型应抛 KeyError。"""
    with pytest.raises(KeyError):
        event("unknown.type", foo="bar")


# ──────────────────────────────────────────────
# T2-3 异常兜底（结构性验证）
# ──────────────────────────────────────────────


def test_research_service_importable():
    """ResearchService 可导入。"""
    from backend.service.research_service import ResearchService
    assert callable(ResearchService)


def test_research_service_stream_research_is_async_generator():
    """stream_research 返回 AsyncGenerator。"""
    from backend.service.research_service import ResearchService
    from inspect import isasyncgenfunction
    assert isasyncgenfunction(ResearchService.stream_research), \
        "stream_research 应为 async generator function"


def test_research_service_no_thread_no_queue():
    """research_service.py 不含 threading.Thread 或 asyncio.Queue（Lock 允许）。"""
    import backend.service.research_service as mod
    import inspect
    src = inspect.getsource(mod)
    assert "threading.Thread" not in src, "不应使用 threading.Thread"
    assert "asyncio.Queue" not in src, "不应使用 asyncio.Queue"
    # Lock 是允许的（用于初始化），但不能有 Thread 或 Event
    assert "Thread(" not in src, "不应创建 Thread 实例"


# ──────────────────────────────────────────────
# T2-4 结束事件必达（mock 验证）
# ──────────────────────────────────────────────


def test_stream_research_emits_started_and_completed():
    """正常路径：run.started + run.completed 必发。"""
    from backend.service.research_service import ResearchService

    svc = ResearchService.__new__(ResearchService)
    svc._initialized = True
    svc._base_config = MagicMock()
    svc._memory_manager = None
    svc._thread_repo = None

    # Mock graph
    async def mock_astream(*args, **kwargs):
        yield ("updates", {"intent": {"intent": "direct", "final": "hello world"}})
        yield ("custom", {"node": "intent", "message": "done"})

    mock_app = MagicMock()
    mock_app.astream = mock_astream
    mock_app.get_state = MagicMock(return_value=MagicMock(
        values={"final": "hello world"}
    ))
    svc._app = mock_app

    # Mock runtime config
    svc._build_runtime_config = MagicMock(return_value=MagicMock(
        thread_id="t1", user_id="u1", tenant_id="tn1", max_iterations=1,
        enable_memory=False, hitl_enabled=False, hitl_config={},
        memory_top_k=3,
    ))
    svc._build_initial_state = MagicMock(return_value={"query": "test"})

    async def collect():
        events = []
        async for sse_line in svc.stream_research("test", "u1", "t1", "tn1"):
            if sse_line.startswith("data: "):
                payload = json.loads(sse_line[6:].strip())
                events.append(payload["type"])
        return events

    types = asyncio.run(collect())
    assert "run.started" in types, "必须发 run.started"
    assert "run.completed" in types, "正常路径必须发 run.completed"


def test_stream_research_emits_error_on_exception():
    """异常路径：run.error 必发，无 finally 挂起。"""
    from backend.service.research_service import ResearchService

    svc = ResearchService.__new__(ResearchService)
    svc._initialized = True
    svc._base_config = MagicMock()
    svc._memory_manager = None
    svc._thread_repo = None

    # Mock graph that throws
    async def mock_astream_error(*args, **kwargs):
        raise RuntimeError("test error")
        yield  # never reached

    mock_app = MagicMock()
    mock_app.astream = mock_astream_error
    svc._app = mock_app

    svc._build_runtime_config = MagicMock(return_value=MagicMock(
        thread_id="t1", user_id="u1", tenant_id="tn1", max_iterations=1,
        enable_memory=False, hitl_enabled=False, hitl_config={},
        memory_top_k=3,
    ))
    svc._build_initial_state = MagicMock(return_value={"query": "test"})

    async def collect():
        events = []
        try:
            async for sse_line in svc.stream_research("test", "u1", "t1", "tn1"):
                if sse_line.startswith("data: "):
                    payload = json.loads(sse_line[6:].strip())
                    events.append(payload)
        except Exception:
            pass
        return events

    events = asyncio.run(collect())
    types = [e["type"] for e in events]
    assert "run.started" in types, "必须发 run.started"
    assert "run.error" in types, "异常路径必须发 run.error"
    # 最后一条应是 run.error（无 finally 挂起）
    assert types[-1] == "run.error", f"最后一条应为 run.error，实际: {types[-1]}"

    # 验证 run.error 的 data 包含 code 和 message
    error_event = next(e for e in events if e["type"] == "run.error")
    assert error_event["data"]["code"] == "RuntimeError"
    assert "test error" in error_event["data"]["message"]


# ──────────────────────────────────────────────
# T2-6 并发不串流（message_id 前缀 run_id 隔离）
# ──────────────────────────────────────────────


def test_message_id_uses_run_id_prefix():
    """message.delta 的 message_id 格式为 {run_id}:{node}。"""
    from backend.service.research_service import ResearchService

    svc = ResearchService.__new__(ResearchService)
    svc._initialized = True
    svc._base_config = MagicMock()
    svc._memory_manager = None
    svc._thread_repo = None

    async def mock_astream(*args, **kwargs):
        yield ("custom", {"type": "token", "node": "write", "text": "hello"})
        yield ("updates", {"write": {"final": "hello"}})

    mock_app = MagicMock()
    mock_app.astream = mock_astream
    mock_app.get_state = MagicMock(return_value=MagicMock(
        values={"final": "hello"}
    ))
    svc._app = mock_app

    svc._build_runtime_config = MagicMock(return_value=MagicMock(
        thread_id="t1", user_id="u1", tenant_id="tn1", max_iterations=1,
        enable_memory=False, hitl_enabled=False, hitl_config={},
        memory_top_k=3,
    ))
    svc._build_initial_state = MagicMock(return_value={"query": "test"})

    async def collect():
        events = []
        async for sse_line in svc.stream_research("test", "u1", "t1", "tn1"):
            if sse_line.startswith("data: "):
                payload = json.loads(sse_line[6:].strip())
                events.append(payload)
        return events

    events = asyncio.run(collect())
    delta_events = [e for e in events if e["type"] == "message.delta"]
    assert len(delta_events) > 0, "应有 message.delta 事件"
    mid = delta_events[0]["data"]["message_id"]
    assert ":write" in mid, f"message_id 应含 :write 后缀，实际: {mid}"
    # message_id 的前缀是 run_id（12位hex）
    prefix = mid.split(":")[0]
    assert len(prefix) == 12, f"run_id 前缀应为12位，实际: {len(prefix)}"
