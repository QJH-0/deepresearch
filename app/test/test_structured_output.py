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


def _tool_call_chunk(args: str):
    return AIMessageChunk(
        content="",
        tool_call_chunks=[
            {"name": "IntentDecision", "args": args, "id": "call_1", "index": 0}
        ],
    )


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

        agent = _agent_yielding(_tool_call_chunk('{"route": "direct", "reason": "问候"}'))

        result, messages = await _invoke_structured_agent(
            {"query": "你好"}, "用户问题：你好", agent, "intent_router", "intent"
        )

        assert isinstance(result, IntentDecision)
        assert result.route == "direct"
        assert len(messages) == 2

    @pytest.mark.asyncio
    async def test_merges_chunked_tool_call_args(self):
        """工具调用参数可能分块到达，必须合并后再校验。"""
        from mult_agents.nodes._parsing import _invoke_structured_agent

        agent = _agent_yielding(
            _tool_call_chunk('{"route": "multi'),
            _tool_call_chunk('agent", "reason": "需检索"}'),
        )

        result, _ = await _invoke_structured_agent(
            {"query": "调研"}, "用户问题：调研", agent, "intent_router", "intent"
        )

        assert result.route == "multiagent"

    @pytest.mark.asyncio
    async def test_raises_when_no_tool_calls(self):
        """模型只回文本、不给工具调用时必须显式失败，不返回兜底值。"""
        from mult_agents.nodes._parsing import StructuredOutputError, _invoke_structured_agent

        agent = _agent_yielding(AIMessageChunk(content="我觉得应该走 direct"))

        with pytest.raises(StructuredOutputError):
            await _invoke_structured_agent(
                {"query": "你好"}, "用户问题：你好", agent, "intent_router", "intent"
            )

    @pytest.mark.asyncio
    async def test_raises_when_args_violate_schema(self):
        """取值不在枚举内必须被 schema 拦下。"""
        from mult_agents.nodes._parsing import StructuredOutputError, _invoke_structured_agent

        agent = _agent_yielding(_tool_call_chunk('{"route": "unknown_route", "reason": "x"}'))

        with pytest.raises(StructuredOutputError):
            await _invoke_structured_agent(
                {"query": "你好"}, "用户问题：你好", agent, "intent_router", "intent"
            )


class TestBuildStructuredAgent:
    def test_binds_schema_without_forcing_tool_choice(self):
        """必须走 bind_tools 而非 response_format —— 后者依赖强制 tool_choice，
        DashScope 会以 InvalidParameter 拒绝。"""
        from mult_agents import models

        bound = MagicMock()
        fake_llm = MagicMock()
        fake_llm.bind_tools.return_value = bound

        with patch.object(models, "_build_llm", return_value=fake_llm):
            agent = models.build_structured_agent(
                "qwen-plus", "", "intent_router", 0.0,
                timeout=60.0, max_retries=2, response_format=IntentDecision,
            )

        fake_llm.bind_tools.assert_called_once_with([IntentDecision])
        assert agent.runnable is bound
        assert agent.schema is IntentDecision
        assert agent.system_prompt

    def test_build_agents_wires_intent_to_structured_agent(self):
        from mult_agents import models
        from mult_agents.models import StructuredAgent

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
             patch.object(models, "build_agent", return_value=MagicMock()):
            models.build_agents("qwen-plus", "", config)

        assert m_structured.call_count == 1
        assert m_structured.call_args.kwargs["response_format"] is IntentDecision


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

