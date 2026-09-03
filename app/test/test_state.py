"""Phase 1/8 测试：State reducer、create_initial_state 完整性、字段校验。

覆盖用例:
    T1-1 State reducer（operator.add / add_messages）
    T1-2 create_initial_state 返回所有必需字段

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_state.py -v
"""

import inspect
import operator
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))

from mult_agents.state import AgentState, create_initial_state  # noqa: E402


# ──────────────────────────────────────────────
# T1-1 State reducer
# ──────────────────────────────────────────────


class TestStateReducers:
    """State 中累加字段使用 operator.add reducer。"""

    def test_sources_reducer_uses_operator_add(self):
        """sources/findings/plan 用 operator.add reducer。"""
        src = inspect.getsource(sys.modules["mult_agents.state"])
        assert "operator.add" in src, "State 中应使用 operator.add reducer"

    def test_messages_uses_add_messages(self):
        """messages 用 add_messages reducer。"""
        src = inspect.getsource(sys.modules["mult_agents.state"])
        assert "add_messages" in src, "messages 应使用 add_messages reducer"

    def test_state_groups_exist(self):
        """State 分组类存在。"""
        from mult_agents.state import ConversationState, ResearchState, ProgressState
        assert hasattr(ConversationState, "__annotations__")
        assert hasattr(ResearchState, "__annotations__")
        assert hasattr(ProgressState, "__annotations__")

    def test_agent_state_inherits_all_groups(self):
        """AgentState 继承所有分组（TypedDict 多重继承合并键）。"""
        from mult_agents.state import ConversationState, ResearchState, ProgressState
        # TypedDict 在运行时合并所有键，验证键集合包含关系
        agent_keys = set(getattr(AgentState, "__annotations__", {}).keys())
        conv_keys = set(getattr(ConversationState, "__annotations__", {}).keys())
        research_keys = set(getattr(ResearchState, "__annotations__", {}).keys())
        progress_keys = set(getattr(ProgressState, "__annotations__", {}).keys())
        assert conv_keys <= agent_keys, f"AgentState 缺少 ConversationState 字段: {conv_keys - agent_keys}"
        assert research_keys <= agent_keys, f"AgentState 缺少 ResearchState 字段: {research_keys - agent_keys}"
        assert progress_keys <= agent_keys, f"AgentState 缺少 ProgressState 字段: {progress_keys - agent_keys}"


# ──────────────────────────────────────────────
# T1-2 create_initial_state 完整性
# ──────────────────────────────────────────────


class TestCreateInitialState:
    """create_initial_state 返回所有必需字段。"""

    REQUIRED_FIELDS = {
        # ConversationState
        "messages", "clarifications",
        # ResearchState
        "query", "user_id", "tenant_id", "memory_context", "intent",
        "plan", "outline", "sub_questions", "research_questions",
        "search_plan", "budget", "supplementary_queries",
        "web_search", "local_rag", "web_evidence", "local_evidence",
        "evidence_pool", "deep_dive", "audit", "audit_flags", "analysis",
        "findings", "claim_map", "source_index", "needs_more_research",
        "missing_gaps", "code", "draft", "final",
        "web_retrieval_stats", "local_retrieval_stats",
        "web_search_trace", "local_rag_trace",
        "hitl_enabled", "hitl_config", "user_feedback", "plan_revision_count",
        # ProgressState
        "phase", "iteration", "max_iterations",
    }

    def test_all_fields_present(self):
        """返回所有必需字段。"""
        state = create_initial_state(
            query="test", max_iterations=3, user_id="u", tenant_id="t"
        )
        for field in self.REQUIRED_FIELDS:
            assert field in state, f"缺少字段: {field}"

    def test_defaults(self):
        """默认值正确。"""
        state = create_initial_state(
            query="test", max_iterations=3, user_id="u", tenant_id="t"
        )
        assert state["messages"] == []
        assert state["clarifications"] == []
        assert state["intent"] == ""
        assert state["phase"] == "initialized"
        assert state["iteration"] == 0
        assert state["max_iterations"] == 3
        assert state["hitl_enabled"] is False
        assert state["plan_revision_count"] == 0

    def test_custom_values(self):
        """自定义值正确传入。"""
        state = create_initial_state(
            query="量子计算调研",
            max_iterations=5,
            user_id="user_001",
            tenant_id="tenant_002",
            memory_context="用户偏好英文",
            hitl_enabled=True,
            hitl_config={"plan_review": False, "analyze_clarify": False, "write_review": True},
        )
        assert state["query"] == "量子计算调研"
        assert state["max_iterations"] == 5
        assert state["user_id"] == "user_001"
        assert state["tenant_id"] == "tenant_002"
        assert state["memory_context"] == "用户偏好英文"
        assert state["hitl_enabled"] is True
        assert state["hitl_config"]["write_review"] is True
        assert state["hitl_config"]["plan_review"] is False

    def test_hitl_config_defaults(self):
        """默认 HITL config 包含三个开关。"""
        state = create_initial_state(
            query="test", max_iterations=3, user_id="u", tenant_id="t"
        )
        config = state["hitl_config"]
        assert "plan_review" in config
        assert "analyze_clarify" in config
        assert "write_review" in config
        assert config["write_review"] is False

    def test_researchstate_compat_alias(self):
        """ResearchStateCompat 是 AgentState 别名。"""
        from mult_agents.state import ResearchStateCompat
        assert ResearchStateCompat is AgentState
