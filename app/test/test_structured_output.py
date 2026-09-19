"""结构化输出测试：决策节点的模型输出必须受 schema 约束。

实现走 create_agent + ProviderStrategy(strict=True)：provider 用 JSON Schema
强制输出，框架校验后放进 state 的 structured_response。
必须显式指定 ProviderStrategy —— 自动策略选择按型号名白名单判断，qwen 不在
白名单内，传裸 schema 会退化成工具调用策略。

本文件覆盖：取结构化结果的正常路径、缺结果、框架校验失败、非结构化异常上抛，
以及构建期对 ProviderStrategy / strict 的约束。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    set PYTHONPATH=app
    python -m pytest app/test/test_structured_output.py -v
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from langchain.agents.structured_output import StructuredOutputError as LangChainStructuredOutputError
from langchain_core.messages import AIMessageChunk

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "app"))

from mult_agents.output_schemas import IntentDecision  # noqa: E402


def _agent_yielding(*, structured_response=None, reasoning="", raise_exc=None):
    """构造结构化执行体替身：astream 以 (mode, chunk) 形式产出。

    对齐 create_agent 的流式契约：messages 模式推消息增量，values 模式推完整 state。
    """

    class _Runnable:
        async def astream(self, _input, stream_mode=None):
            if raise_exc is not None:
                raise raise_exc
            if reasoning:
                yield ("messages", (AIMessageChunk(content="", reasoning_content=reasoning), {}))
            if structured_response is not None:
                yield ("values", {"structured_response": structured_response})

    return SimpleNamespace(runnable=_Runnable(), schema=IntentDecision)


class TestInvokeStructuredAgent:
    @pytest.mark.asyncio
    async def test_returns_structured_response(self):
        from mult_agents.nodes._parsing import _invoke_structured_agent

        decision = IntentDecision(route="direct", reason="问候")
        agent = _agent_yielding(structured_response=decision)

        result, messages = await _invoke_structured_agent(
            {"query": "你好"}, "用户问题：你好", agent, "intent_router", "intent"
        )

        assert result is decision
        assert len(messages) == 2

    @pytest.mark.asyncio
    async def test_raises_when_structured_response_missing(self):
        """state 里没有 structured_response 时必须显式失败，不返回兜底值。"""
        from mult_agents.nodes._parsing import StructuredOutputError, _invoke_structured_agent

        agent = _agent_yielding()

        with pytest.raises(StructuredOutputError):
            await _invoke_structured_agent(
                {"query": "你好"}, "用户问题：你好", agent, "intent_router", "intent"
            )

    @pytest.mark.asyncio
    async def test_converts_framework_validation_error(self):
        """框架报的 schema 校验失败要转成节点认识的异常，供其决定是否降级。"""
        from mult_agents.nodes._parsing import StructuredOutputError, _invoke_structured_agent

        agent = _agent_yielding(
            raise_exc=LangChainStructuredOutputError("取值不在枚举内")
        )

        with pytest.raises(StructuredOutputError):
            await _invoke_structured_agent(
                {"query": "你好"}, "用户问题：你好", agent, "intent_router", "intent"
            )

    @pytest.mark.asyncio
    async def test_non_structured_error_propagates(self):
        """网络/超时类异常必须原样上抛 —— 它们不是「结构化失败」，不该被降级掩盖。"""
        from mult_agents.nodes._parsing import StructuredOutputError, _invoke_structured_agent

        agent = _agent_yielding(raise_exc=ConnectionError("连接中断"))

        with pytest.raises(ConnectionError):
            await _invoke_structured_agent(
                {"query": "你好"}, "用户问题：你好", agent, "intent_router", "intent"
            )

    @pytest.mark.asyncio
    async def test_pushes_reasoning_as_thinking_event(self):
        """reasoning 增量仍要透出为 thinking 事件（通道支持时才有内容）。"""
        from mult_agents.nodes._parsing import _invoke_structured_agent

        events = []
        agent = _agent_yielding(
            reasoning="先看是否涉及检索", structured_response=IntentDecision(route="multiagent")
        )

        await _invoke_structured_agent(
            {"query": "调研"}, "用户问题：调研", agent, "intent_router", "intent",
            writer=events.append,
        )

        assert any(e.get("type") == "thinking" and e.get("text") == "先看是否涉及检索" for e in events)


class TestBuildStructuredAgent:
    def test_uses_provider_strategy_with_strict(self):
        """必须显式用 ProviderStrategy + strict：裸 schema 会退化成工具调用策略。"""
        from langchain.agents.structured_output import ProviderStrategy

        from mult_agents import models

        captured = {}

        def fake_create_agent(**kwargs):
            captured.update(kwargs)
            return MagicMock()

        with patch.object(models, "_build_llm", return_value=MagicMock()), \
             patch.object(models, "create_agent", fake_create_agent):
            agent = models.build_structured_agent(
                "qwen3.8-max", "", "intent_router", 0.0,
                timeout=60.0, max_retries=2, response_format=IntentDecision,
            )

        response_format = captured["response_format"]
        assert isinstance(response_format, ProviderStrategy)
        assert response_format.schema is IntentDecision

        payload = response_format.to_model_kwargs()["response_format"]
        assert payload["type"] == "json_schema"
        assert payload["json_schema"]["strict"] is True
        assert payload["json_schema"]["name"] == "IntentDecision"
        assert "route" in payload["json_schema"]["schema"]["properties"]

        assert captured["tools"] == []
        assert captured["system_prompt"], "system prompt 由 create_agent 注入，不在执行体上重复保存"
        assert agent.schema is IntentDecision

    def test_rejects_model_without_json_schema_support(self):
        """不支持 json_schema 的型号必须启动即失败，而不是运行期才报 schema 错。"""
        from mult_agents import models

        with pytest.raises(ValueError, match="不支持 response_format 的 json_schema 模式"):
            models.build_structured_agent(
                "qwen-plus", "", "intent_router", 0.0,
                timeout=60.0, max_retries=2, response_format=IntentDecision,
            )

    def test_supports_json_schema_covers_five_families(self):
        from mult_agents.models import JSON_SCHEMA_MODELS, supports_json_schema

        assert set(JSON_SCHEMA_MODELS) == {
            "qwen3.7-plus", "qwen3.7-flash", "qwen3.7-max",
            "qwen3.8-flash", "qwen3.8-max",
        }
        # 快照版本号后缀同样算支持
        assert supports_json_schema("qwen3.8-max-0902")
        assert not supports_json_schema("qwen-plus")

    def test_build_agents_wires_schema_bound_nodes(self):
        """结构化节点各自绑定 schema，其余节点仍走 create_agent。"""
        from mult_agents import models
        from mult_agents.models import StructuredAgent
        from mult_agents.output_schemas import (
            AnalysisDraft,
            DeepDiveDraft,
            IntentDecision,
            LocalRagDraft,
            PlanDraft,
            RetrievalGradeDraft,
            WebSearchDraft,
        )

        config = MagicMock()
        config.milvus_host = ""
        config.milvus_port = 19530
        config.postgres_dsn = ""
        config.llm_timeout_seconds = 60.0
        config.llm_max_retries = 2
        config.thinking_nodes = []
        config.node_models = {}

        with patch.object(models, "init_rag_system"), \
             patch.object(models, "build_structured_agent", return_value=MagicMock(spec=StructuredAgent)) as m_structured, \
             patch.object(models, "build_agent", return_value=MagicMock()) as m_agent:
            models.build_agents("qwen-plus", "", config)

        wired = {call.args[2]: call.kwargs["response_format"] for call in m_structured.call_args_list}
        assert wired == {
            "intent_router": IntentDecision,
            "plan": PlanDraft,
            "web_search": WebSearchDraft,
            "local_rag": LocalRagDraft,
            "retrieve_grader": RetrievalGradeDraft,
            "deep_dive": DeepDiveDraft,
            "analyze": AnalysisDraft,
        }
        # 面向用户的正文产出仍走自由文本，不结构化
        assert {call.args[2] for call in m_agent.call_args_list} == {
            "direct_answer", "write", "clarify",
        }


class TestStructuredPromptDoesNotMandateFormat:
    """结构化节点的提示词不得规定输出格式。

    实测教训：intent_router 原文含「你必须只输出 JSON，格式固定为 {...}」时，
    模型会照着提示词回一段文本 JSON，而不是发起工具调用 —— 结果是
    tool_calls 为空、结构化路径判定失败、静默退回规则引擎。
    格式由 schema 约束，提示词只描述判断标准。
    """

    def test_intent_router_prompt_has_no_json_format_mandate(self):
        from mult_agents.prompts import PROMPTS

        prompt = PROMPTS["intent_router"]

        assert "只输出 JSON" not in prompt
        assert '"route"' not in prompt



class TestDecisionNodesConsumeStructuredResult:
    """决策节点直接消费 schema 对象，不再走「解析 dict + 类型兜底」分支。"""

    @pytest.mark.asyncio
    async def test_plan_node_maps_schema_to_state(self, monkeypatch):
        from mult_agents.nodes import plan
        from mult_agents.output_schemas import OutlineSection, PlanDraft

        async def fake_invoke(state, prompt, agent, agent_name, node, writer=None):
            return (
                PlanDraft(
                    objective="目标",
                    sub_questions=["Q1", "Q2"],
                    outline=[OutlineSection(id="sec_1", title="章节一", search_queries=["k"])],
                    research_questions=["RQ"],
                    budget={"max_rounds": 5},
                ),
                [],
            )

        monkeypatch.setattr(plan, "_invoke_structured_agent", fake_invoke)

        out = await plan.plan_node({"query": "q", "hitl_enabled": False}, None, "planner")

        assert out["plan"] == "目标"
        assert out["sub_questions"] == ["Q1", "Q2"]
        assert out["outline"][0]["id"] == "sec_1"
        assert out["budget"]["max_rounds"] == 5

    @pytest.mark.asyncio
    async def test_plan_node_degrades_to_default_plan(self, monkeypatch):
        from mult_agents.nodes import plan
        from mult_agents.nodes._parsing import StructuredOutputError

        async def fake_invoke(state, prompt, agent, agent_name, node, writer=None):
            raise StructuredOutputError("未返回结构化结果")

        monkeypatch.setattr(plan, "_invoke_structured_agent", fake_invoke)

        out = await plan.plan_node({"query": "原问题", "hitl_enabled": False}, None, "planner")

        assert out["plan"] == "原问题"
        assert out["sub_questions"] == ["原问题"]
        assert out["agent_messages"] == []

    @pytest.mark.asyncio
    async def test_analyze_node_degrades_to_fallback(self, monkeypatch):
        from mult_agents.nodes import analyze
        from mult_agents.nodes._parsing import StructuredOutputError

        async def fake_invoke(state, prompt, agent, agent_name, node, writer=None):
            raise StructuredOutputError("schema 不匹配")

        monkeypatch.setattr(analyze, "_invoke_structured_agent", fake_invoke)

        out = await analyze.analyze_node(
            {
                "query": "q",
                "hitl_enabled": False,
                "sub_questions": [],
                "evidence_pool": [],
                "audit_flags": [],
            },
            None,
            "analyst",
        )

        assert out["needs_more_research"] is False
        assert out["missing_gaps"] == []
        assert len(out["findings"]) == 1

    @pytest.mark.asyncio
    async def test_analyze_emits_gap_queries_and_advances_iteration(self, monkeypatch):
        """B3：缺口检索词与轮次推进都由 analyze 负责（原 reflect 节点的职责）。

        state 要能被 checkpointer 序列化，schema 对象必须先 dump 成 dict。
        """
        import json

        from mult_agents.nodes import analyze
        from mult_agents.output_schemas import AnalysisDraft, SupplementaryQuery

        async def fake_invoke(state, prompt, agent, agent_name, node, writer=None):
            return (
                AnalysisDraft(
                    analysis_summary="分析",
                    needs_more_research=True,
                    missing_gaps=["缺口A"],
                    gap_queries=[
                        SupplementaryQuery(section_id="gap_1", query="补检词", source_preference="web")
                    ],
                ),
                [],
            )

        monkeypatch.setattr(analyze, "_invoke_structured_agent", fake_invoke)

        out = await analyze.analyze_node(
            {
                "query": "q", "iteration": 0, "max_iterations": 3, "hitl_enabled": False,
                "sub_questions": [], "evidence_pool": [], "audit_flags": [],
            },
            None,
            "analyst",
        )

        assert out["supplementary_queries"] == [
            {"section_id": "gap_1", "query": "补检词", "source_preference": "web", "reason": ""}
        ]
        json.dumps(out["supplementary_queries"])
        assert out["next_action"] == "reflect"
        assert out["iteration"] == 1, "继续研究才推进轮次"

    @pytest.mark.asyncio
    async def test_analyze_stops_at_iteration_cap(self, monkeypatch):
        """已达迭代上限时不得推进轮次，否则上限闸永远不生效。"""
        from mult_agents.nodes import analyze
        from mult_agents.output_schemas import AnalysisDraft, SupplementaryQuery

        async def fake_invoke(state, prompt, agent, agent_name, node, writer=None):
            return (
                AnalysisDraft(
                    analysis_summary="分析",
                    needs_more_research=True,
                    gap_queries=[SupplementaryQuery(section_id="g", query="q")],
                ),
                [],
            )

        monkeypatch.setattr(analyze, "_invoke_structured_agent", fake_invoke)

        out = await analyze.analyze_node(
            {
                "query": "q", "iteration": 3, "max_iterations": 3, "hitl_enabled": False,
                "sub_questions": [], "evidence_pool": [], "audit_flags": [],
            },
            None,
            "analyst",
        )

        assert out["next_action"] == "write"
        assert out["iteration"] == 3, "达上限时不得推进轮次"
        assert out["supplementary_queries"] == []
