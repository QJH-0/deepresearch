"""检索词派生回归测试 — 修复「请调研」被误识别为检索实体。

背景（BUG）：
    _guess_primary_entity 曾把「请调研」当作检索实体，生成「请调研是什么」等
    垃圾查询词，导致搜索引擎返回汉字「请」的无关结果 → 证据池为空 → 无报告。

覆盖用例：
    R1-1 指令动词剥离 — _guess_primary_entity 跳过「请调研」返回真实实体
    R1-2 直接检索词 — _derive_direct_search_queries 不再产出「请调研是什么」
    B4   检索计划排序 — 短检索词优先、长句压短兜底（原 R1-3 的「子问题优先」已被 B4 反转）
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
    """B4：计划按「先宽后窄」排序 —— LLM 的短检索词优先，长句子问题兜底。

    此前顺序是**反的**：长句 `sub_questions` 排第一，而计划上限只有 6 条，
    sub_questions 一多，LLM 专为检索生成的短词就全被挤掉了。
    """

    def test_llm_search_queries_come_first(self):
        from mult_agents.nodes._evidence import _derive_search_plan

        outline = [{
            "id": "sec_1",
            "title": "主流 AI Agent 框架的技术演进",
            "description": "LangGraph、AutoGen 等框架的架构对比",
            "search_queries": ["LangGraph 状态机", "AutoGen 多智能体"],
        }]
        subs = ["【核心原问题】2024年主流AI Agent框架在架构设计上呈现出哪些核心发展趋势"]

        plan = _derive_search_plan(outline, subs, [], "2024年AI Agent框架发展趋势调研")

        assert [p["query"] for p in plan[:2]] == ["LangGraph 状态机", "AutoGen 多智能体"], (
            "LLM 专为检索生成的短词应排最前"
        )

    def test_grounding_accepts_query_matching_its_own_section(self):
        """检索词只要对得上所属章节的主题就合法，不要求与用户原句同词。"""
        from mult_agents.nodes._evidence import _derive_search_plan

        outline = [{
            "id": "sec_1",
            "title": "LangGraph 状态机编排",
            "search_queries": ["LangGraph 状态机"],
        }]

        plan = _derive_search_plan(outline, [], [], "2024年AI Agent框架发展趋势调研")

        assert [p["query"] for p in plan[:1]] == ["LangGraph 状态机"]

    def test_rejects_query_unrelated_to_topic_and_section(self):
        """与话题、子问题、所属章节都对不上的检索词仍应被挡掉。"""
        from mult_agents.nodes._evidence import _derive_search_plan

        outline = [{"id": "sec_1", "title": "AI Agent 框架", "search_queries": ["如何选购冰箱"]}]

        plan = _derive_search_plan(outline, [], [], "2024年AI Agent框架发展趋势调研")

        assert all(p["query"] != "如何选购冰箱" for p in plan)

    def test_sub_questions_are_condensed_and_used_as_fallback(self):
        from mult_agents.nodes._evidence import _derive_search_plan

        long_sub = (
            "【核心原问题】2024年主流AI Agent框架（如LangChain、LlamaIndex等）"
            "在架构设计上呈现出哪些核心发展趋势"
        )
        plan = _derive_search_plan([], [long_sub], [], "2024年AI Agent框架发展趋势调研")

        assert plan, "无 LLM 检索词时应回落到子问题"
        assert all(len(p["query"]) <= 40 for p in plan), "回落时也必须压到可检索的长度"
        assert "【" not in plan[0]["query"], "结构性标注应被剥掉"

    def test_fallback_when_empty(self):
        from mult_agents.nodes._evidence import _derive_search_plan

        plan = _derive_search_plan([], [], [], "企业知识库平台")
        assert len(plan) >= 1
        assert plan[0]["query"]


class TestCondenseQuery:
    """Anthropic 的实测结论：agent 默认给出又长又具体的查询，返回结果极少。"""

    def test_strips_annotations_and_question_tail(self):
        from mult_agents.nodes._evidence import _condense_query

        result = _condense_query("【核心原问题】AI Agent 框架有哪些发展趋势")

        assert "【" not in result
        assert "有哪些" not in result
        assert "AI Agent 框架" in result

    def test_keeps_short_query_untouched(self):
        from mult_agents.nodes._evidence import _condense_query

        assert _condense_query("LangGraph 状态机") == "LangGraph 状态机"

    def test_cuts_at_clause_boundary_not_mid_word(self):
        from mult_agents.nodes._evidence import _condense_query

        result = _condense_query(
            "RAG与Fine-tuning技术路线对比分析，需要给出成本、适用场景与迁移代价的量化对比"
        )

        assert len(result) <= 40
        assert result == "RAG与Fine-tuning技术路线对比分析", "应在子句边界截断，而不是把词切一半"

    def test_grounding_uses_sub_questions_too(self):
        """只用原问题判接地会误杀与子问题相关的合法检索词。"""
        from mult_agents.nodes._evidence import _is_query_grounded

        assert _is_query_grounded(
            "LangGraph 状态机", "2024年AI Agent框架发展趋势调研 LangGraph 多智能体协同"
        )
        assert not _is_query_grounded("LangGraph 状态机", "2024年AI Agent框架发展趋势调研")
