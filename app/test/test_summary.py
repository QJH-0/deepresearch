"""对话摘要压缩服务测试。

覆盖用例:
    T-SUM-1 模块导入 — SummaryService / get_summary_service / init_summary_service 可导入
    T-SUM-2 单例模式 — init 后 get 返回同一实例
    T-SUM-3 未触发摘要 — 消息数 <= threshold 原样返回
    T-SUM-4 触发摘要 — 消息数 > threshold 触发压缩，保留最近消息
    T-SUM-5 空消息列表 — 不触发摘要
    T-SUM-6 已有摘要追加 — existing_summary 被追加到新摘要前
    T-SUM-7 LLM 失败降级 — 摘要调用失败时返回原始消息
    T-SUM-8 State 字段 — conversation_summary 存在于初始状态
    T-SUM-9 配置项 — AppConfig 包含 summary_threshold / summary_keep_recent / summary_model

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    set PYTHONPATH=app
    python -m pytest app/test/test_summary.py -v
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
import sys
sys.path.insert(0, str(_APP_PATH))


# ── T-SUM-1: 模块导入 ──────────────────────────────────

class TestSummaryImport:
    """T-SUM-1: 摘要服务模块可正常导入。"""

    def test_import_summary_service(self):
        from backend.service.summary_service import (
            SummaryService,
            get_summary_service,
            init_summary_service,
        )
        assert SummaryService is not None
        assert callable(get_summary_service)
        assert callable(init_summary_service)

    def test_import_from_service_package(self):
        from backend.service import SummaryService, get_summary_service
        assert SummaryService is not None
        assert callable(get_summary_service)


# ── T-SUM-2: 单例模式 ──────────────────────────────────

class TestSummarySingleton:
    """T-SUM-2: init 后 get 返回同一实例。"""

    def test_singleton_after_init(self):
        from backend.service.summary_service import (
            init_summary_service,
            get_summary_service,
        )
        svc = init_summary_service(api_key="test-key", model="qwen-turbo", threshold=15, keep_recent=4)
        svc2 = get_summary_service()
        assert svc is svc2
        assert svc._threshold == 15
        assert svc._keep_recent == 4

    def test_get_before_init_returns_none(self):
        from backend.service import summary_service
        summary_service._SERVICE = None
        assert summary_service.get_summary_service() is None


# ── T-SUM-3: 未触发摘要 ──────────────────────────────────

class TestSummaryNoTrigger:
    """T-SUM-3: 消息数 <= threshold 不触发摘要。"""

    @pytest.mark.asyncio
    async def test_below_threshold_no_summary(self):
        from backend.service.summary_service import SummaryService
        from langchain_core.messages import HumanMessage, AIMessage

        svc = SummaryService(api_key="test", threshold=10, keep_recent=4)
        msgs = [HumanMessage(content=f"msg {i}") for i in range(5)]
        msgs.append(AIMessage(content="reply"))

        result_msgs, summary = await svc.summarize_if_needed(msgs, "")
        assert result_msgs is msgs
        assert summary == ""

    @pytest.mark.asyncio
    async def test_equal_threshold_no_summary(self):
        from backend.service.summary_service import SummaryService
        from langchain_core.messages import HumanMessage

        svc = SummaryService(api_key="test", threshold=5, keep_recent=2)
        msgs = [HumanMessage(content=f"m{i}") for i in range(5)]

        result_msgs, summary = await svc.summarize_if_needed(msgs, "")
        assert len(result_msgs) == 5
        assert summary == ""


# ── T-SUM-4: 触发摘要 ──────────────────────────────────

class TestSummaryTrigger:
    """T-SUM-4: 消息数 > threshold 触发压缩。"""

    @pytest.mark.asyncio
    async def test_triggers_summary_above_threshold(self):
        from backend.service.summary_service import SummaryService
        from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

        svc = SummaryService(api_key="test", threshold=5, keep_recent=2)

        msgs = []
        for i in range(8):
            msgs.append(HumanMessage(content=f"question {i}"))
            msgs.append(AIMessage(content=f"answer {i}"))

        with patch.object(
            svc, "_generate_summary", new_callable=AsyncMock, return_value="这是摘要内容"
        ):
            result_msgs, summary = await svc.summarize_if_needed(msgs, "")

        assert len(result_msgs) == 3
        assert isinstance(result_msgs[0], SystemMessage)
        assert "对话摘要" in result_msgs[0].content
        assert "这是摘要内容" in result_msgs[0].content
        assert summary == "这是摘要内容"
        assert result_msgs[1:] == msgs[-2:]

    @pytest.mark.asyncio
    async def test_keep_recent_preserved(self):
        from backend.service.summary_service import SummaryService
        from langchain_core.messages import HumanMessage, AIMessage

        svc = SummaryService(api_key="test", threshold=4, keep_recent=3)
        msgs = [HumanMessage(content=f"m{i}") for i in range(8)]

        with patch.object(
            svc, "_generate_summary", new_callable=AsyncMock, return_value="summary"
        ):
            result_msgs, _ = await svc.summarize_if_needed(msgs, "")

        recent = result_msgs[-3:]
        assert [m.content for m in recent] == ["m5", "m6", "m7"]


# ── T-SUM-5: 空消息列表 ──────────────────────────────────

class TestSummaryEmpty:
    """T-SUM-5: 空消息列表不触发摘要。"""

    @pytest.mark.asyncio
    async def test_empty_messages(self):
        from backend.service.summary_service import SummaryService

        svc = SummaryService(api_key="test", threshold=10, keep_recent=4)
        result_msgs, summary = await svc.summarize_if_needed([], "")
        assert result_msgs == []
        assert summary == ""


# ── T-SUM-6: 已有摘要追加 ──────────────────────────────────

class TestSummaryAppend:
    """T-SUM-6: existing_summary 被追加到新摘要前。"""

    @pytest.mark.asyncio
    async def test_existing_summary_appended(self):
        from backend.service.summary_service import SummaryService
        from langchain_core.messages import HumanMessage

        svc = SummaryService(api_key="test", threshold=3, keep_recent=2)
        msgs = [HumanMessage(content=f"m{i}") for i in range(6)]
        existing = "之前的摘要内容"

        with patch.object(
            svc, "_generate_summary", new_callable=AsyncMock, return_value="新摘要"
        ):
            _, summary = await svc.summarize_if_needed(msgs, existing)

        assert "之前的摘要内容" in summary
        assert "新摘要" in summary
        assert summary.index("之前的摘要内容") < summary.index("新摘要")


# ── T-SUM-7: LLM 失败降级 ──────────────────────────────────

class TestSummaryLLMFailure:
    """T-SUM-7: LLM 调用失败时返回原始消息列表。"""

    @pytest.mark.asyncio
    async def test_llm_failure_fallback(self):
        from backend.service.summary_service import SummaryService
        from langchain_core.messages import HumanMessage

        svc = SummaryService(api_key="test", threshold=3, keep_recent=2)
        msgs = [HumanMessage(content=f"m{i}") for i in range(6)]

        with patch.object(
            svc, "_generate_summary", new_callable=AsyncMock, return_value=""
        ):
            result_msgs, summary = await svc.summarize_if_needed(msgs, "")

        assert result_msgs is msgs
        assert summary == ""


# ── T-SUM-8: State 字段 ──────────────────────────────────

class TestSummaryStateField:
    """T-SUM-8: conversation_summary 存在于初始状态。"""

    def test_initial_state_has_summary_field(self):
        from mult_agents.state import create_initial_state

        state = create_initial_state(
            query="test",
            max_iterations=3,
            user_id="u1",
            tenant_id="t1",
        )
        assert "conversation_summary" in state
        assert state["conversation_summary"] == ""

    def test_conversation_state_typeddict_has_field(self):
        from mult_agents.state import ConversationState
        assert "conversation_summary" in ConversationState.__annotations__


# ── T-SUM-9: 配置项 ──────────────────────────────────

class TestSummaryConfig:
    """T-SUM-9: AppConfig 包含摘要相关配置字段。"""

    def test_config_has_summary_fields(self):
        from mult_agents.config import AppConfig
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(AppConfig)}
        assert "summary_threshold" in field_names
        assert "summary_keep_recent" in field_names
        assert "summary_model" in field_names

    def test_settings_has_summary_fields(self):
        from backend.config.settings import BusinessSettings

        biz = BusinessSettings()
        assert hasattr(biz, "summary_threshold")
        assert hasattr(biz, "summary_keep_recent")
        assert hasattr(biz, "summary_model")
        assert biz.summary_threshold == 20
        assert biz.summary_keep_recent == 6
        assert biz.summary_model == "qwen-turbo"


# ── T-SUM-10: _role_name 辅助方法 ─────────────────────────

class TestSummaryRoleName:
    """T-SUM-10: _role_name 正确识别消息角色。"""

    def test_role_name_mapping(self):
        from backend.service.summary_service import SummaryService
        from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

        assert SummaryService._role_name(HumanMessage(content="x")) == "用户"
        assert SummaryService._role_name(AIMessage(content="x")) == "助手"
        assert SummaryService._role_name(SystemMessage(content="x")) == "系统"

    def test_truncate(self):
        from backend.service.summary_service import SummaryService

        assert SummaryService._truncate("short", 10) == "short"
        assert SummaryService._truncate("a" * 20, 10) == "a" * 10 + "..."
