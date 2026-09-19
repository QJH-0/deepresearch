"""Phase 1 测试：State reducer、节点契约、模型工厂、DDG 降级、拓扑校验。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    set PYTHONPATH=app
    python -m pytest app/test/test_p1.py -v
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "app"))

from mult_agents.state import AgentState, create_initial_state  # noqa: E402


# ──────────────────────────────────────────────
# T1-1 State reducer
# ──────────────────────────────────────────────


def test_state_sources_reducer():
    """sources/plan/findings/clarifications 均为累加 reducer。"""
    import operator
    from mult_agents.state import AgentState
    # TypedDict 不直接支持运行时 reducer 测试，但我们可以验证 State 定义中
    # 使用了 Annotated[list, operator.add]
    import inspect
    src = inspect.getsource(sys.modules["mult_agents.state"])
    assert "operator.add" in src, "State 中应使用 operator.add reducer"
    assert "add_messages" in src, "messages 应使用 add_messages reducer"


def test_create_initial_state_has_all_fields():
    """create_initial_state 返回所有必需字段。"""
    state = create_initial_state(
        query="test query",
        max_iterations=3,
        user_id="user1",
        tenant_id="tenant1",
    )
    assert state["query"] == "test query"
    assert state["max_iterations"] == 3
    assert state["user_id"] == "user1"
    assert state["chat_messages"] == []
    assert state["agent_messages"] == []
    assert state["clarifications"] == []
    assert state["intent"] == ""
    assert state["phase"] == "initialized"


# ──────────────────────────────────────────────
# T1-3 重复实现已合并
# ──────────────────────────────────────────────


def test_fallback_analysis_unique_definition():
    """_fallback_analysis 全局唯一定义（grep 计数=1）。"""
    import subprocess
    result = subprocess.run(
        ["git", "grep", "-r", "-c", "def _fallback_analysis", "--", "app/"],
        capture_output=True, text=True, cwd=str(_PROJECT_ROOT),
    )
    # 每个文件只出现一次
    for line in result.stdout.strip().split("\n"):
        if line:
            parts = line.split(":")
            count = int(parts[-1])
            assert count == 1, f"{parts[0]} 中 _fallback_analysis 定义次数={count}"


def test_no_hypotheses_references():
    """app/ 源码中 hypotheses 引用零命中（排除测试文件）。"""
    import subprocess
    result = subprocess.run(
        ["git", "grep", "-r", "-l", "hypotheses", "--", "app/"],
        capture_output=True, text=True, cwd=str(_PROJECT_ROOT),
    )
    lines = [
        l for l in result.stdout.strip().split("\n")
        if l and "test_" not in l
    ]
    assert len(lines) == 0, f"仍有 hypotheses 引用: {lines}"


# ──────────────────────────────────────────────
# T1-9 拓扑不变
# ──────────────────────────────────────────────


def test_graph_topology_has_clarify():
    """graph.get_graph().nodes 集合包含全部节点。

    刻意**不用 try/except 兜底**：此前这里 `except (TypeError, Exception)` 后
    退化成「检查 graph.py 源码里出现过节点名」，结果 build_app 真的编译失败时
    测试仍然通过 —— B1 加 grader 时就踩过这个坑（条件边目标传了 list，
    compile 抛 unhashable type，却被兜底吞掉）。图编译失败必须让测试失败。
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from mult_agents.graph import build_app
    from mult_agents.runtime import AgentBundle

    # 用 mock agents 避免 LLM 初始化
    mock_agents = AgentBundle(
        intent_router=None, planner=None, scout_web=None,
        scout_local=None, retrieval_grader=None, evidence_judge=None, analyst=None,
        direct_responder=None, writer=None, clarifier=None,
    )
    app = build_app(mock_agents, InMemorySaver())
    node_names = set(app.get_graph().nodes.keys())

    expected_nodes = {
        "__start__", "__end__",
        "intent", "direct_answer", "clarify", "plan",
        "web_search", "local_rag", "retrieve_grader", "deep_dive",
        "analyze", "write",
    }
    assert expected_nodes <= node_names, f"缺少节点: {expected_nodes - node_names}"


def test_retrieval_inner_loop_topology():
    """锁定检索内层循环的边。

    此前没有任何测试锁定边集合，拓扑改动只能靠人看 diff。B1 引入 grader 后必须锁住，
    否则「重检回环被删」「检索绕过 grader 直连 deep_dive」这类退化不会被发现。
    """
    from langgraph.checkpoint.memory import InMemorySaver
    from mult_agents.graph import build_app
    from mult_agents.runtime import AgentBundle

    mock_agents = AgentBundle(
        intent_router=None, planner=None, scout_web=None,
        scout_local=None, retrieval_grader=None, evidence_judge=None, analyst=None,
        direct_responder=None, writer=None, clarifier=None,
    )
    edges = {
        (edge.source, edge.target)
        for edge in build_app(mock_agents, InMemorySaver()).get_graph().edges
    }

    expected = {
        ("plan", "web_search"),
        ("plan", "local_rag"),
        ("web_search", "retrieve_grader"),
        ("local_rag", "retrieve_grader"),
        ("retrieve_grader", "web_search"),   # 不充分 → 扇出回两条检索边
        ("retrieve_grader", "local_rag"),
        ("retrieve_grader", "deep_dive"),    # 充分/达上限 → 出检索阶段
        ("deep_dive", "analyze"),
        ("analyze", "web_search"),   # 需继续研究 → 扇出回两条检索边
        ("analyze", "local_rag"),
        ("analyze", "write"),        # 证据充分/达上限 → 成文
    }
    assert expected <= edges, f"缺少边: {expected - edges}"
    assert ("web_search", "deep_dive") not in edges, (
        "检索必须经 retrieve_grader 才能进 deep_dive，否则自适应重检被绕过"
    )
    assert ("local_rag", "deep_dive") not in edges


# ──────────────────────────────────────────────
# T1-6 DDG 429 注入（mock）
# ──────────────────────────────────────────────


def test_ddg_search_failure_returns_empty():
    """mock provider 抛限流异常 → 返回空列表、不抛异常。"""
    from mult_agents.tools import DuckDuckGoProvider

    provider = DuckDuckGoProvider()
    # Mock _ddgs() 返回的 DDGS 实例的 .text() 抛异常
    mock_ddgs = MagicMock()
    mock_ddgs.text.side_effect = Exception("429 Too Many Requests")
    provider._ddgs = lambda: mock_ddgs

    result = asyncio.run(provider.search("test query", max_results=5))
    assert result == [], f"异常时应返回空列表，实际: {result}"


def test_search_provider_protocol():
    """DuckDuckGoProvider 实现 SearchProvider Protocol。"""
    from mult_agents.tools import SearchProvider, DuckDuckGoProvider
    provider = DuckDuckGoProvider()
    assert isinstance(provider, SearchProvider), "DuckDuckGoProvider 应实现 SearchProvider Protocol"


# ──────────────────────────────────────────────
# T1-7 Redis 缓存命中（mock）
# ──────────────────────────────────────────────


def test_ddg_cache_hit():
    """同 query 二次调用不触发 DDGS().text（mock 计数=1）。"""
    from mult_agents.tools import DuckDuckGoProvider

    mock_redis = MagicMock()
    cached_data = [{"title": "cached", "url": "http://cached.com", "snippet": "", "source_type": "web"}]
    mock_redis.get = AsyncMock(return_value='[{"title": "cached", "url": "http://cached.com", "snippet": "", "source_type": "web"}]')
    mock_redis.setex = AsyncMock()

    provider = DuckDuckGoProvider(redis_client=mock_redis)

    # 第一次调用（缓存命中，不触发 DDGS）
    result = asyncio.run(provider.search("test", max_results=5))
    assert len(result) == 1
    assert result[0]["title"] == "cached"

    # DDGS().text 不应被调用（因为缓存命中了）
    mock_ddgs = MagicMock()
    mock_ddgs.text = MagicMock(return_value=[])
    provider._ddgs = lambda: mock_ddgs

    result2 = asyncio.run(provider.search("test", max_results=5))
    assert mock_ddgs.text.call_count == 0, "缓存命中时不应调用 DDGS().text()"


# ──────────────────────────────────────────────
# T1-4 模型工厂
# ──────────────────────────────────────────────


def test_models_import():
    """models.py 可导入 build_agents / build_agent。"""
    from mult_agents.models import build_agents, build_agent
    assert callable(build_agents)
    assert callable(build_agent)


# ──────────────────────────────────────────────
# T1-2 节点契约（导入验证）
# ──────────────────────────────────────────────


# ──────────────────────────────────────────────
# LLM 调用韧性（timeout / max_retries 由配置驱动）
# ──────────────────────────────────────────────


def test_llm_resilience_defaults():
    """超时与重试必须有配置默认值，避免长尾请求拖死整轮研究。"""
    import dataclasses

    from mult_agents.config import AppConfig

    defaults = {
        f.name: f.default
        for f in dataclasses.fields(AppConfig)
        if f.default is not dataclasses.MISSING
    }

    assert defaults["llm_timeout_seconds"] == 60.0
    assert defaults["llm_max_retries"] == 2


def test_llm_resilience_env_override(monkeypatch):
    from mult_agents.config import AppConfig

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "5.5")
    monkeypatch.setenv("LLM_MAX_RETRIES", "7")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")

    cfg = AppConfig.from_file()

    assert cfg.llm_timeout_seconds == 5.5
    assert cfg.llm_max_retries == 7


def test_all_nodes_importable():
    """每个节点可从 nodes 包导入。"""
    from mult_agents.nodes import (
        intent_node,
        direct_answer_node,
        plan_node,
        web_search_node,
        local_rag_node,
        deep_dive_node,
        analyze_node,
        write_node,
        clarify_node,
        bind_agent,
    )
    assert all(callable(f) for f in [
        intent_node, direct_answer_node, plan_node, web_search_node,
        local_rag_node, deep_dive_node, analyze_node,
        write_node, clarify_node, bind_agent,
    ])


# ──────────────────────────────────────────────
# 检索超时（搜索源不可达时快速失败）
# ──────────────────────────────────────────────


class _HangingChain:
    """模拟搜索源不可达：链会一直挂着，只能靠外层超时收口。"""

    def __init__(self):
        self.cancelled = False

    async def search(self, query, max_results=5):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return []


def test_search_timeout_default_when_settings_unavailable(monkeypatch):
    """读不到配置时回落到 15 秒，而不是无上限。"""
    from mult_agents import tools

    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr("backend.config.settings.get_business_settings", _boom)

    assert tools._search_timeout_seconds() == 15.0


def test_web_search_records_times_out_in_loop(monkeypatch):
    """有运行中事件循环时（生产路径）：超时返回空结果，并取消挂起的检索。

    不取消的话，检索会连同连接一起留在常驻后台循环上，多次超时会不断累积。
    """
    import time

    from mult_agents import tools

    chain = _HangingChain()
    monkeypatch.setattr(tools, "_get_provider_chain", lambda: chain)
    monkeypatch.setattr(tools, "_search_timeout_seconds", lambda: 0.2)

    async def _call():
        return tools.web_search_records("q", 3)

    assert asyncio.run(_call()) == []

    deadline = time.time() + 2
    while time.time() < deadline and not chain.cancelled:
        time.sleep(0.05)
    assert chain.cancelled, "超时后必须取消挂起的检索"


def test_web_search_records_times_out_without_running_loop(monkeypatch):
    """同步上下文调用同样受超时约束（此前该分支完全没有超时）。"""
    from mult_agents import tools

    monkeypatch.setattr(tools, "_get_provider_chain", lambda: _HangingChain())
    monkeypatch.setattr(tools, "_search_timeout_seconds", lambda: 0.2)

    assert tools.web_search_records("q", 3) == []
