"""检索词派生回归测试 — 修复「请调研」被误识别为检索实体。

背景（BUG）：
    _guess_primary_entity 曾把「请调研」当作检索实体，生成「请调研是什么」等
    垃圾查询词，导致搜索引擎返回汉字「请」的无关结果 → 证据池为空 → 无报告。

覆盖用例：
    R1-1 指令动词剥离 — _guess_primary_entity 跳过「请调研」返回真实实体
    R1-2 直接检索词 — _derive_direct_search_queries 不再产出「请调研是什么」
    R1-3 检索计划 — _derive_search_plan 优先注入 planner 子问题
"""

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))


# ──────────────────────────────────────────────
# R1-1 指令动词剥离
# ──────────────────────────────────────────────


class TestGuessPrimaryEntity:
    def test_strips_instruction_verb(self):
        from mult_agents.nodes._evidence import _guess_primary_entity

        query = '请调研"企业知识库 Agent 平台"市场，按市场规模、主要竞品、收费模式三部分输出。'
        assert _guess_primary_entity(query) == "企业知识库"

    def test_strips_help_prefix(self):
        from mult_agents.nodes._evidence import _guess_primary_entity

        assert _guess_primary_entity("帮我分析知识库平台竞品") == "知识库平台竞品"

    def test_strips_analyse_verb(self):
        from mult_agents.nodes._evidence import _guess_primary_entity

        assert _guess_primary_entity("分析语音分离模型") == "语音分离模型"

    def test_ascii_entity_priority(self):
        from mult_agents.nodes._evidence import _guess_primary_entity

        assert _guess_primary_entity("调研 LangGraph 的架构") == "langgraph"

    def test_empty_returns_empty(self):
        from mult_agents.nodes._evidence import _guess_primary_entity

        assert _guess_primary_entity("请") == ""
        assert _guess_primary_entity("调研") == ""


# ──────────────────────────────────────────────
# R1-2 直接检索词
# ──────────────────────────────────────────────


class TestDeriveDirectSearchQueries:
    def test_no_garbage_instruction_query(self):
        from mult_agents.nodes._evidence import _derive_direct_search_queries

        query = '请调研"企业知识库 Agent 平台"市场，按市场规模、主要竞品、收费模式三部分输出。'
        queries = _derive_direct_search_queries(query)
        # 不再出现「请调研是什么」「请调研 GitHub」等垃圾词
        assert not any("请调研" in q for q in queries)
        # 首个检索词应剥离「请调研」前缀
        assert not queries[0].startswith("请调研")

    def test_entity_based_expansion(self):
        from mult_agents.nodes._evidence import _derive_direct_search_queries

        queries = _derive_direct_search_queries("介绍语音分离技术")
        assert any("语音分离技术是什么" == q for q in queries)

    def test_strips_verb_particle(self):
        from mult_agents.nodes._evidence import _guess_primary_entity

        # 「介绍一下X」→ 剥离「介绍」+「一下」→ 实体 X
        assert _guess_primary_entity("介绍一下语音分离技术") == "语音分离技术"


# ──────────────────────────────────────────────
# R1-3 检索计划优先子问题
# ──────────────────────────────────────────────


class TestDeriveSearchPlan:
    def test_sub_questions_injected_first(self):
        from mult_agents.nodes._evidence import _derive_search_plan

        subs = [
            "企业知识库 Agent 平台市场规模与增长率",
            "主流平台竞品对比",
            "收费模式与定价案例",
        ]
        plan = _derive_search_plan([], subs, [], "调研企业知识库 Agent 平台市场")
        section_ids = [p["section_id"] for p in plan]
        # 子问题应排在最前
        assert section_ids[:3] == ["sub_question"] * 3
        # 子问题检索词与输入一致
        assert [p["query"] for p in plan[:3]] == subs

    def test_fallback_when_empty(self):
        from mult_agents.nodes._evidence import _derive_search_plan

        plan = _derive_search_plan([], [], [], "企业知识库平台")
        assert len(plan) >= 1
        assert plan[0]["query"]
