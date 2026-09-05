"""流式节点契约测试（P0-1 回归防护）。

两条不变式：
1. langgraph 仅对注解为「裸 StreamWriter」的参数注入运行时 writer；
   `Optional[StreamWriter]` 注解会导致不注入，节点内 writer 恒为 None。
2. writer 缺失时（直接调用 / 注入失败），astream token 仍必须累加进内容，
   不得静默丢弃后触发降级 ainvoke（重复调用 LLM）。
"""
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langgraph.types import StreamWriter

from mult_agents.nodes.analyze import analyze_node, reflect_node
from mult_agents.nodes.clarify import clarify_node
from mult_agents.nodes.deep_dive import deep_dive_node
from mult_agents.nodes.intent import direct_answer_node, intent_node
from mult_agents.nodes.local_rag import local_rag_node
from mult_agents.nodes.plan import plan_node
from mult_agents.nodes.web_search import web_search_node
from mult_agents.nodes.write import write_node

STREAM_NODES = [
    analyze_node, clarify_node, deep_dive_node, direct_answer_node,
    intent_node, local_rag_node, plan_node, reflect_node,
    web_search_node, write_node,
]

STREAM_TEXT = "流式正文"


def _astream_agent():
    agent = MagicMock()
    agent.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="降级内容")]})

    async def fake_astream(*args, **kwargs):
        yield (MagicMock(content=STREAM_TEXT), {})

    agent.astream = fake_astream
    return agent


def test_writer_annotation_is_bare_streamwriter():
    for fn in STREAM_NODES:
        param = inspect.signature(fn).parameters["writer"]
        assert param.annotation is StreamWriter, (
            f"{fn.__name__}.writer 注解须为裸 StreamWriter，"
            f"实测 {param.annotation!r}（Optional 注解会导致 langgraph 不注入 writer）"
        )


BASE_STATE = {
    "query": "q", "sub_questions": [], "findings": [], "source_index": [],
    "audit_flags": [], "sources": [], "messages": [],
    "web_retrieval_stats": {"query_count": 1, "raw_count": 3, "kept_count": 2},
    "local_retrieval_stats": {"query_count": 0, "raw_count": 0, "kept_count": 0},
    "hitl_enabled": False, "hitl_config": {}, "thread_id": "t",
    "iteration": 1, "max_iterations": 3,
}

_WRITE_PATCHES = (
    patch("mult_agents.nodes.write._check_evidence_sufficiency", return_value=(True, "")),
    patch("mult_agents.nodes.write._validate_and_fix_citations", return_value=(STREAM_TEXT, [])),
    patch("mult_agents.nodes.write._ensure_reference_section", return_value=STREAM_TEXT),
)


@pytest.mark.asyncio
async def test_write_node_keeps_tokens_without_writer():
    agent = _astream_agent()
    with contextlib_exit(_WRITE_PATCHES):
        result = await write_node(dict(BASE_STATE), agent, "test_agent")
    assert agent.ainvoke.await_count == 0, "astream 已产出内容时不得触发降级 ainvoke"
    assert STREAM_TEXT in result["final"]


@pytest.mark.asyncio
async def test_direct_answer_node_keeps_tokens_without_writer():
    agent = _astream_agent()
    result = await direct_answer_node(dict(BASE_STATE), agent, "test_agent")
    assert agent.ainvoke.await_count == 0, "astream 已产出内容时不得触发降级 ainvoke"
    assert STREAM_TEXT in result["final"]


class contextlib_exit:
    """轻量多 patch 上下文（避免引入 contextlib.ExitStack 噪音）。"""

    def __init__(self, patches):
        self._patches = patches

    def __enter__(self):
        return [p.__enter__() for p in self._patches]

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.__exit__(*exc)
        return False
