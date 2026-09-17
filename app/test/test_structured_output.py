"""结构化输出测试：决策节点的模型输出必须受 schema 约束。

背景：DashScope 只接受 tool_choice 为 none/auto，因此 langchain 1.x 的
create_agent(response_format=...) 不可用（实测报 InvalidParameter）。
决策节点改为「绑定 schema → 模型以工具调用产出 → 按 schema 校验」，
本文件覆盖该路径的成功与两类失败。

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
from langchain_core.messages import AIMessageChunk

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "app"))

from mult_agents.output_schemas import IntentDecision  # noqa: E402


def _json_chunk(text: str):
    """JSON Schema 模式下正文即 JSON 片段。"""
    return AIMessageChunk(content=text)


def _agent_yielding(*chunks):
    """构造结构化执行体替身：astream 逐块产出给定 chunk。"""

    class _Runnable:
        async def astream(self, messages):
            for chunk in chunks:
                yield chunk

    return SimpleNamespace(
        runnable=_Runnable(),
        system_prompt="你是意图路由器",
        schema=IntentDecision,
    )


class TestInvokeStructuredAgent:
    @pytest.mark.asyncio
    async def test_returns_validated_model(self):
        from mult_agents.nodes._parsing import _invoke_structured_agent

        agent = _agent_yielding(_json_chunk('{"route": "direct", "reason": "问候"}'))

        result, messages = await _invoke_structured_agent(
            {"query": "你好"}, "用户问题：你好", agent, "intent_router", "intent"
        )

        assert isinstance(result, IntentDecision)
        assert result.route == "direct"
        assert len(messages) == 2

    @pytest.mark.asyncio
    async def test_merges_chunked_json(self):
        """JSON 正文会分块到达，必须累加后再反序列化。"""
        from mult_agents.nodes._parsing import _invoke_structured_agent

        agent = _agent_yielding(
            _json_chunk('{"route": "multi'),
            _json_chunk('agent", "reason": "需检索"}'),
        )

        result, _ = await _invoke_structured_agent(
            {"query": "调研"}, "用户问题：调研", agent, "intent_router", "intent"
        )

        assert result.route == "multiagent"

    @pytest.mark.asyncio
    async def test_raises_when_content_empty(self):
        """模型没产出正文时必须显式失败，不返回兜底值。"""
        from mult_agents.nodes._parsing import StructuredOutputError, _invoke_structured_agent

        agent = _agent_yielding(AIMessageChunk(content=""))

        with pytest.raises(StructuredOutputError):
            await _invoke_structured_agent(
                {"query": "你好"}, "用户问题：你好", agent, "intent_router", "intent"
            )

    @pytest.mark.asyncio
    async def test_raises_when_content_is_not_json(self):
        """正文不是 JSON 说明 provider 没遵守 schema，必须暴露而不是宽容解析。"""
        from mult_agents.nodes._parsing import StructuredOutputError, _invoke_structured_agent

        agent = _agent_yielding(_json_chunk("我觉得应该走 direct"))

        with pytest.raises(StructuredOutputError):
            await _invoke_structured_agent(
                {"query": "你好"}, "用户问题：你好", agent, "intent_router", "intent"
            )

    @pytest.mark.asyncio
    async def test_raises_when_value_violates_schema(self):
        """取值不在枚举内必须被 schema 拦下。"""
        from mult_agents.nodes._parsing import StructuredOutputError, _invoke_structured_agent

        agent = _agent_yielding(_json_chunk('{"route": "unknown_route", "reason": "x"}'))

        with pytest.raises(StructuredOutputError):
            await _invoke_structured_agent(
                {"query": "你好"}, "用户问题：你好", agent, "intent_router", "intent"
            )


class TestBuildStructuredAgent:
    def test_binds_json_schema_response_format(self):
        """结构化节点走 provider 的 json_schema 模式，而不是工具调用变通方案。"""
        from mult_agents import models

        bound = MagicMock()
        fake_llm = MagicMock()
        fake_llm.bind.return_value = bound

        with patch.object(models, "_build_llm", return_value=fake_llm):
            agent = models.build_structured_agent(
                "qwen3.8-max", "", "intent_router", 0.0,
                timeout=60.0, max_retries=2, response_format=IntentDecision,
            )

        response_format = fake_llm.bind.call_args.kwargs["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is True
        assert response_format["json_schema"]["name"] == "IntentDecision"
        assert "route" in response_format["json_schema"]["schema"]["properties"]
        assert agent.runnable is bound
        assert agent.schema is IntentDecision
        assert agent.system_prompt

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

    def test_build_agents_wires_decision_nodes_to_structured_agents(self):
        """四个决策节点走结构化执行体，其余节点仍走 create_agent。"""
        from mult_agents import models
        from mult_agents.models import StructuredAgent
        from mult_agents.output_schemas import AnalysisDraft, IntentDecision, PlanDraft, ReflectionDraft

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
            "reflect": ReflectionDraft,
            "analyze": AnalysisDraft,
        }
        # 证据类节点仍是自由文本产出，未结构化
        assert {call.args[2] for call in m_agent.call_args_list} == {
            "web_search", "local_rag", "deep_dive", "direct_answer", "write", "clarify",
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
    async def test_reflect_node_stores_plain_dicts(self, monkeypatch):
        """state 要能被 checkpointer 序列化，schema 对象必须先 dump 成 dict。"""
        import json

        from mult_agents.nodes import analyze
        from mult_agents.output_schemas import ReflectionDraft, SupplementaryQuery

        async def fake_invoke(state, prompt, agent, agent_name, node, writer=None):
            return (
                ReflectionDraft(
                    reflection_summary="补搜",
                    supplementary_queries=[
                        SupplementaryQuery(section_id="gap_1", query="q2", source_preference="web")
                    ],
                ),
                [],
            )

        monkeypatch.setattr(analyze, "_invoke_structured_agent", fake_invoke)

        out = await analyze.reflect_node(
            {"query": "q", "iteration": 0, "missing_gaps": ["g"], "supplementary_queries": []},
            None,
            "reflect",
        )

        assert out["supplementary_queries"] == [
            {"section_id": "gap_1", "query": "q2", "source_preference": "web", "reason": ""}
        ]
        json.dumps(out["supplementary_queries"])
        assert out["iteration"] == 1
