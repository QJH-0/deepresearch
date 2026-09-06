"""R2.3 LLM 驱动澄清测试。

覆盖用例:
    T2.3-01 规则快速通道行为不变
    T2.3-02 LLM 判定需澄清
    T2.3-03 LLM 判定无需澄清直接进 plan
    T2.3-04 LLM 异常静默放行
    T2.3-05 JSON 解析容错
    T2.3-06 回答充分性判定与追问
    T2.3-07 轮次上限放行
    T2.3-08 agent 注入（build_agents 包含 clarifier）

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_clarify_llm.py -v
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))


# ──────────────────────────────────────────────
# 辅助：构造 mock agent
# ──────────────────────────────────────────────


def _make_mock_agent(content: str):
    """构造一个 mock agent，invoke 返回包含指定 content 的消息。"""
    agent = MagicMock()
    msg = MagicMock()
    msg.content = content
    agent.invoke.return_value = {"messages": [msg]}
    return agent


def _make_failing_agent():
    """构造一个 invoke 会抛异常的 mock agent。"""
    agent = MagicMock()
    agent.invoke.side_effect = RuntimeError("LLM 调用失败")
    return agent


_INTERRUPT_TARGET = "mult_agents.nodes._shared.interrupt"


# ──────────────────────────────────────────────
# T2.3-01 规则快速通道行为不变
# ──────────────────────────────────────────────


class TestRuleFastPath:
    """规则快速通道命中时直接发起澄清，LLM 未被调用。"""

    def test_ambiguous_query_triggers_interrupt_without_llm(self):
        from mult_agents.nodes.clarify import clarify_node

        agent = _make_mock_agent('{"needs_clarification": false, "confidence": 0.9}')
        state = {"query": "最近的一些进展", "clarifications": [], "clarify_rounds": 0}

        with patch(_INTERRUPT_TARGET, return_value=["用户回答"]) as mock_intr:
            result = clarify_node(state, agent=agent, agent_name="clarifier")

        assert mock_intr.call_count == 1
        agent.invoke.assert_not_called()
        payload = mock_intr.call_args[0][0]
        assert payload["kind"] == "clarification"

    def test_short_query_triggers_interrupt_without_llm(self):
        from mult_agents.nodes.clarify import clarify_node

        agent = _make_mock_agent('{"needs_clarification": false, "confidence": 0.9}')
        state = {"query": "ai", "clarifications": [], "clarify_rounds": 0}

        with patch(_INTERRUPT_TARGET, return_value=["回答"]) as mock_intr:
            result = clarify_node(state, agent=agent, agent_name="clarifier")

        assert mock_intr.call_count == 1
        agent.invoke.assert_not_called()


# ──────────────────────────────────────────────
# T2.3-02 LLM 判定需澄清
# ──────────────────────────────────────────────


class TestLLMNeedsClarification:
    """LLM 判定需要澄清时发起中断，payload 含 options。"""

    def test_llm_needs_clarification_triggers_interrupt(self):
        from mult_agents.nodes.clarify import clarify_node

        llm_output = (
            '{"needs_clarification": true, "confidence": 0.3, '
            '"reason": "范围不明", '
            '"questions": [{"question": "时间范围？", "options": ["近一年", "不限"]}]}'
        )
        agent = _make_mock_agent(llm_output)
        state = {"query": "研究AI发展", "clarifications": [], "clarify_rounds": 0}

        with patch(_INTERRUPT_TARGET, return_value=["近一年"]) as mock_intr:
            result = clarify_node(state, agent=agent, agent_name="clarifier")

        assert mock_intr.call_count == 1
        payload = mock_intr.call_args[0][0]
        assert payload["kind"] == "clarification"
        questions = payload["questions"]
        assert len(questions) == 1
        assert len(questions[0]["options"]) == 2


# ──────────────────────────────────────────────
# T2.3-03 LLM 判定无需澄清直接进 plan
# ──────────────────────────────────────────────


class TestLLMNoClarification:
    """LLM 判定无需澄清时直接进入 plan。"""

    def test_llm_no_clarification_goes_to_plan(self):
        from mult_agents.nodes.clarify import clarify_node

        llm_output = '{"needs_clarification": false, "confidence": 0.9, "reason": "需求明确"}'
        agent = _make_mock_agent(llm_output)
        state = {"query": "请调研2024年人工智能领域的技术发展趋势", "clarifications": [], "clarify_rounds": 0}

        with patch(_INTERRUPT_TARGET) as mock_intr:
            result = clarify_node(state, agent=agent, agent_name="clarifier")

        mock_intr.assert_not_called()
        assert result.goto == "plan"


# ──────────────────────────────────────────────
# T2.3-04 LLM 异常静默放行
# ──────────────────────────────────────────────


class TestLLMExceptionSilentPass:
    """LLM 调用异常时静默放行进入 plan。"""

    def test_llm_exception_goes_to_plan(self):
        from mult_agents.nodes.clarify import clarify_node

        agent = _make_failing_agent()
        state = {"query": "请调研2024年人工智能领域的技术发展趋势", "clarifications": [], "clarify_rounds": 0}

        with patch(_INTERRUPT_TARGET) as mock_intr:
            result = clarify_node(state, agent=agent, agent_name="clarifier")

        mock_intr.assert_not_called()
        assert result.goto == "plan"


# ──────────────────────────────────────────────
# T2.3-05 JSON 解析容错
# ──────────────────────────────────────────────


class TestJSONParsing:
    """JSON 解析容错：markdown 代码块包裹的 JSON 能正确解析。"""

    def test_extract_json_from_markdown_block(self):
        from mult_agents.nodes.clarify import _extract_json

        text = '```json\n{"needs_clarification": true, "confidence": 0.3}\n```'
        result = _extract_json(text)
        assert result is not None
        assert result["needs_clarification"] is True
        assert result["confidence"] == 0.3

    def test_extract_json_plain_text_returns_none(self):
        from mult_agents.nodes.clarify import _extract_json

        result = _extract_json("这不是 JSON 格式的文本")
        assert result is None

    def test_extract_json_plain_json(self):
        from mult_agents.nodes.clarify import _extract_json

        result = _extract_json('{"key": "value"}')
        assert result is not None
        assert result["key"] == "value"

    def test_extract_json_with_surrounding_text(self):
        from mult_agents.nodes.clarify import _extract_json

        text = '好的，结果如下：\n{"needs_clarification": false, "confidence": 0.9}\n以上。'
        result = _extract_json(text)
        assert result is not None
        assert result["needs_clarification"] is False

    def test_llm_clarify_verdict_with_markdown_wrapper(self):
        from mult_agents.nodes.clarify import _llm_clarify_verdict

        llm_output = (
            '```json\n{"needs_clarification": true, "confidence": 0.35, '
            '"reason": "时间不明确", '
            '"questions": [{"question": "时间范围？", "options": ["近一年", "不限"]}]}\n```'
        )
        agent = _make_mock_agent(llm_output)
        verdict = _llm_clarify_verdict(agent, "研究AI", [])
        assert verdict is not None
        assert verdict["needs_clarification"] is True
        assert len(verdict["questions"]) == 1
        assert len(verdict["questions"][0]["options"]) == 2

    def test_llm_clarify_verdict_plain_text_returns_none(self):
        from mult_agents.nodes.clarify import _llm_clarify_verdict

        agent = _make_mock_agent("这不是 JSON")
        verdict = _llm_clarify_verdict(agent, "研究AI", [])
        assert verdict is None


# ──────────────────────────────────────────────
# T2.3-06 回答充分性判定与追问
# ──────────────────────────────────────────────


class TestAnswerSufficiency:
    """回答充分性判定：不充分且未超轮次 → 追问。"""

    def test_insufficient_answer_triggers_followup(self):
        from mult_agents.nodes.clarify import clarify_node

        llm_output = (
            '{"sufficient": false, "reason": "仍缺目标用户", '
            '"followup_questions": [{"question": "目标用户是？", "options": ["开发者", "管理层"]}]}'
        )
        agent = _make_mock_agent(llm_output)
        state = {
            "query": "研究AI发展",
            "clarifications": [{"q": [{"question": "时间范围？", "options": []}], "a": ["近一年"]}],
            "clarify_rounds": 1,
        }

        with patch(_INTERRUPT_TARGET, return_value=["开发者"]) as mock_intr:
            result = clarify_node(state, agent=agent, agent_name="clarifier")

        assert mock_intr.call_count == 1
        payload = mock_intr.call_args[0][0]
        assert payload["kind"] == "clarification"
        questions = payload["questions"]
        assert len(questions) == 1
        assert questions[0]["question"] == "目标用户是？"

    def test_sufficient_answer_goes_to_plan(self):
        from mult_agents.nodes.clarify import clarify_node

        llm_output = '{"sufficient": true, "reason": "信息充分"}'
        agent = _make_mock_agent(llm_output)
        state = {
            "query": "研究AI发展",
            "clarifications": [{"q": [{"question": "时间范围？", "options": []}], "a": ["近一年"]}],
            "clarify_rounds": 1,
        }

        with patch(_INTERRUPT_TARGET) as mock_intr:
            result = clarify_node(state, agent=agent, agent_name="clarifier")

        mock_intr.assert_not_called()
        assert result.goto == "plan"

    def test_sufficiency_failure_goes_to_plan(self):
        from mult_agents.nodes.clarify import clarify_node

        agent = _make_failing_agent()
        state = {
            "query": "研究AI发展",
            "clarifications": [{"q": [{"question": "时间范围？", "options": []}], "a": ["近一年"]}],
            "clarify_rounds": 1,
        }

        with patch(_INTERRUPT_TARGET) as mock_intr:
            result = clarify_node(state, agent=agent, agent_name="clarifier")

        mock_intr.assert_not_called()
        assert result.goto == "plan"


# ──────────────────────────────────────────────
# T2.3-07 轮次上限放行
# ──────────────────────────────────────────────


class TestMaxRoundsPass:
    """轮次达到上限时直接放行进入 plan。"""

    def test_max_rounds_goes_to_plan(self):
        from mult_agents.nodes.clarify import clarify_node

        agent = _make_mock_agent('{"needs_clarification": true, "confidence": 0.2}')
        state = {
            "query": "研究AI发展",
            "clarifications": [],
            "clarify_rounds": 2,
            "hitl_config": {"clarify_max_rounds": 2},
        }

        with patch(_INTERRUPT_TARGET) as mock_intr:
            result = clarify_node(state, agent=agent, agent_name="clarifier")

        mock_intr.assert_not_called()
        assert result.goto == "plan"

    def test_followup_at_max_rounds_goes_to_plan(self):
        from mult_agents.nodes.clarify import clarify_node

        llm_output = (
            '{"sufficient": false, "reason": "仍不足", '
            '"followup_questions": [{"question": "再问？", "options": ["A", "B"]}]}'
        )
        agent = _make_mock_agent(llm_output)
        state = {
            "query": "研究AI发展",
            "clarifications": [{"q": [{"question": "时间？", "options": []}], "a": ["近一年"]}],
            "clarify_rounds": 2,
            "hitl_config": {"clarify_max_rounds": 2},
        }

        with patch(_INTERRUPT_TARGET) as mock_intr:
            result = clarify_node(state, agent=agent, agent_name="clarifier")

        mock_intr.assert_not_called()
        assert result.goto == "plan"


# ──────────────────────────────────────────────
# T2.3-08 agent 注入
# ──────────────────────────────────────────────


class TestAgentInjection:
    """build_agents 构建的 AgentBundle 包含 clarifier agent。"""

    def test_bundle_has_clarifier(self):
        from mult_agents.runtime import AgentBundle

        fields = {f.name for f in AgentBundle.__dataclass_fields__.values()}
        assert "clarifier" in fields

    def test_clarify_node_bound_with_agent(self):
        """graph 中 clarify 节点通过 bind_agent 绑定了 clarifier。"""
        from mult_agents.nodes._shared import bind_agent
        from mult_agents.nodes.clarify import clarify_node
        from functools import partial

        mock_agent = MagicMock()
        bound = bind_agent(clarify_node, mock_agent, "clarifier")
        assert isinstance(bound, partial)
        assert bound.keywords.get("agent") is mock_agent
        assert bound.keywords.get("agent_name") == "clarifier"


# ──────────────────────────────────────────────
# 补充：confidence clamp 测试
# ──────────────────────────────────────────────


class TestConfidenceClamp:
    """confidence 值 clamp 到 [0, 1]。"""

    def test_confidence_above_1_clamped(self):
        from mult_agents.nodes.clarify import _llm_clarify_verdict

        agent = _make_mock_agent(
            '{"needs_clarification": false, "confidence": 1.5}'
        )
        verdict = _llm_clarify_verdict(agent, "测试查询", [])
        assert verdict is not None
        assert verdict["confidence"] == 1.0

    def test_confidence_below_0_clamped(self):
        from mult_agents.nodes.clarify import _llm_clarify_verdict

        agent = _make_mock_agent(
            '{"needs_clarification": false, "confidence": -0.3}'
        )
        verdict = _llm_clarify_verdict(agent, "测试查询", [])
        assert verdict is not None
        assert verdict["confidence"] == 0.0


# ──────────────────────────────────────────────
# 补充：state clarify_rounds 初始化
# ──────────────────────────────────────────────


class TestStateClarifyRounds:
    """create_initial_state 初始化 clarify_rounds 为 0。"""

    def test_initial_state_has_clarify_rounds_zero(self):
        from mult_agents.state import create_initial_state

        state = create_initial_state(
            query="test", max_iterations=3, user_id="u", tenant_id="t"
        )
        assert state["clarify_rounds"] == 0
