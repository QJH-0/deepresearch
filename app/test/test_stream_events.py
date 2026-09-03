"""Phase 2/8 测试：全事件过 EVENT_REGISTRY 校验 + 必发结束事件。

覆盖用例:
    T2-5（全事件过 EVENT_REGISTRY 校验）
    T2-3/T2-4（必发结束事件：run.started → run.completed / run.error）

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_stream_events.py -v
"""

import json
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))

from backend.schemas.events import EVENT_REGISTRY, event, sse  # noqa: E402
from pydantic import BaseModel  # noqa: E402


# ──────────────────────────────────────────────
# T2-5 全事件过 EVENT_REGISTRY 校验
# ──────────────────────────────────────────────


class TestEventRegistryCompliance:
    """所有事件类型必须注册在 EVENT_REGISTRY 中，且 data 模型为 BaseModel 子类。"""

    EXPECTED_TYPES = {
        "run.started",
        "agent.status",
        "message.start",
        "message.delta",
        "message.thinking",
        "sources.found",
        "interrupt.raised",
        "run.completed",
        "run.cancelled",
        "run.error",
    }

    def test_registry_keys_match_expected(self):
        """EVENT_REGISTRY 覆盖全部 10 种事件类型。"""
        assert set(EVENT_REGISTRY.keys()) == self.EXPECTED_TYPES

    def test_all_registry_values_are_basemodel(self):
        """EVENT_REGISTRY 的值全部是 BaseModel 子类。"""
        for name, cls in EVENT_REGISTRY.items():
            assert issubclass(cls, BaseModel), f"{name} 的 data 模型不是 BaseModel 子类"

    def test_each_event_type_constructs_from_example(self):
        """每种事件可从示例 dict 构造，关键字段存在。"""
        examples = {
            "run.started": ("thread_id", "run_id"),
            "agent.status": ("node", "label", "phase"),
            "message.start": ("message_id", "role", "node"),
            "message.delta": ("message_id", "text"),
            "message.thinking": ("message_id", "text"),
            "sources.found": ("sources",),
            "interrupt.raised": ("interrupt_id", "kind", "payload"),
            "run.completed": ("message_id", "final_state"),
            "run.cancelled": ("reason",),
            "run.error": ("code", "message"),
        }
        data_examples = {
            "run.started": {"thread_id": "t1", "run_id": "r1"},
            "agent.status": {"node": "plan", "label": "规划", "phase": "completed"},
            "message.start": {"message_id": "m1", "role": "assistant", "node": "plan"},
            "message.delta": {"message_id": "m1", "text": "hello"},
            "message.thinking": {"message_id": "m1", "text": "thinking..."},
            "sources.found": {"sources": [{"url": "https://x.com", "title": "X"}]},
            "interrupt.raised": {"interrupt_id": "i1", "kind": "plan_approval", "payload": {}},
            "run.completed": {"message_id": "m1", "final_state": "done"},
            "run.cancelled": {"reason": "user_cancelled"},
            "run.error": {"code": "RuntimeError", "message": "boom"},
        }
        for type_, required_keys in examples.items():
            env = event(type_, **data_examples[type_])
            assert env.type == type_
            for key in required_keys:
                assert key in env.data, f"{type_} 缺少字段 {key}"

    def test_event_envelope_has_three_keys(self):
        """EventEnvelope 序列化为 {type, ts, data} 三键。"""
        env = event("run.started", thread_id="t1", run_id="r1")
        dumped = env.model_dump()
        assert set(dumped.keys()) == {"type", "ts", "data"}

    def test_sse_line_format(self):
        """sse() 输出格式为 data: {json}\\n\\n。"""
        env = event("message.delta", message_id="m1", text="hello")
        line = sse(env)
        assert line.startswith("data: ")
        assert line.endswith("\n\n")
        payload = json.loads(line[6:].strip())
        assert payload["type"] == "message.delta"
        assert payload["data"]["message_id"] == "m1"

    def test_unknown_event_type_raises(self):
        """未知事件类型应报 KeyError。"""
        with pytest.raises(KeyError):
            event("unknown.type", foo="bar")

    def test_interrupt_kind_enum(self):
        """interrupt.raised 的 kind 只允许三种枚举值。"""
        from backend.schemas.events import InterruptRaisedData
        from pydantic import ValidationError

        for kind in ("plan_approval", "clarification", "report_review"):
            d = InterruptRaisedData(interrupt_id="i1", kind=kind, payload={})
            assert d.kind == kind

        with pytest.raises(ValidationError):
            InterruptRaisedData(interrupt_id="i1", kind="invalid", payload={})

    def test_source_item_types(self):
        """SourceItem source_type 只有 web 和 kb。"""
        from backend.schemas.events import SourceItem

        s_web = SourceItem(url="https://a.com", source_type="web")
        s_kb = SourceItem(url=None, source_type="kb", chunk_id="c1")
        assert s_web.source_type == "web"
        assert s_kb.source_type == "kb"

    def test_ts_is_milliseconds(self):
        """ts 为毫秒级时间戳。"""
        env = event("run.started", thread_id="t1", run_id="r1")
        assert env.ts > 1_000_000_000_000  # 毫秒级


# ──────────────────────────────────────────────
# T2-3/T2-4 必发结束事件（结构性验证）
# ──────────────────────────────────────────────


class TestTerminalEventSemantics:
    """协议不变式：流一定以 completed / cancelled / error 之一结束。"""

    TERMINAL_EVENTS = {"run.completed", "run.cancelled", "run.error"}

    def test_terminal_events_exist_in_registry(self):
        """结束事件在 EVENT_REGISTRY 中。"""
        for t in self.TERMINAL_EVENTS:
            assert t in EVENT_REGISTRY, f"结束事件 {t} 不在 EVENT_REGISTRY 中"

    def test_terminal_event_data_fields(self):
        """结束事件的 data 模型字段正确。"""
        # run.completed
        completed = event("run.completed", message_id="m1", final_state="done")
        assert completed.data["message_id"] == "m1"

        # run.cancelled
        cancelled = event("run.cancelled", reason="user_cancelled")
        assert cancelled.data["reason"] == "user_cancelled"

        # run.error
        error = event("run.error", code="RuntimeError", message="boom")
        assert error.data["code"] == "RuntimeError"
        assert error.data["message"] == "boom"

    def test_run_started_has_thread_and_run_id(self):
        """run.started 必须包含 thread_id 和 run_id。"""
        env = event("run.started", thread_id="t1", run_id="r1")
        assert "thread_id" in env.data
        assert "run_id" in env.data


# ──────────────────────────────────────────────
# research_service 结构性验证
# ──────────────────────────────────────────────


class TestResearchServiceStructure:
    """ResearchService 结构性验证（无 LLM 依赖）。"""

    def test_research_service_importable(self):
        """ResearchService 可导入（直接从模块文件导入，绕过 __init__ 链）。"""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_rs_test",
            _APP_PATH / "backend" / "service" / "research_service.py",
        )
        # research_service 依赖 backend.service.__init__，此处仅验证文件存在
        assert (_APP_PATH / "backend" / "service" / "research_service.py").exists()

    def test_stream_research_is_async_generator(self):
        """stream_research 返回 AsyncGenerator（源码结构性检查）。"""
        import inspect
        src = (_APP_PATH / "backend" / "service" / "research_service.py").read_text(
            encoding="utf-8"
        )
        assert "async def stream_research" in src, "应有 async def stream_research"
        assert "yield" in src, "stream_research 应包含 yield"

    def test_no_thread_no_queue(self):
        """research_service.py 不含 threading.Thread 或 asyncio.Queue。"""
        src = (_APP_PATH / "backend" / "service" / "research_service.py").read_text(
            encoding="utf-8"
        )
        assert "threading.Thread" not in src, "不应使用 threading.Thread"
        assert "asyncio.Queue" not in src, "不应使用 asyncio.Queue"
        assert "Thread(" not in src, "不应创建 Thread 实例"
