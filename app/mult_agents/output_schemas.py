"""决策节点的模型输出 schema。

这些节点只产出「下一步怎么走」的判断，不含证据正文，因此适合让模型直接
产出受 schema 约束的结果，而不是解析自由文本再兜底。

字段约束的用意：把提示词里的软要求（字数上限、取值枚举）变成模型侧与校验侧
共用的硬约束 —— 提示词会被模型忽略，schema 不会。
"""

from typing import Literal

from pydantic import BaseModel, Field


class IntentDecision(BaseModel):
    """意图路由决策。"""

    route: Literal["direct", "multiagent"] = Field(description="路由目标")
    reason: str = Field(default="", max_length=200, description="判断理由")


class OutlineSection(BaseModel):
    """研究大纲的一节。"""

    id: str = Field(description="章节标识，如 sec_1")
    title: str = Field(description="章节标题")
    description: str = Field(default="", description="本章要回答什么")
    section_type: str = Field(default="mixed", description="章节类型")
    requires_data: bool = Field(default=False, description="是否需要数据支撑")
    requires_chart: bool = Field(default=False, description="是否需要图表")
    priority: int = Field(default=1, description="执行优先级")
    search_queries: list[str] = Field(default_factory=list, description="本章的检索词")
    status: str = Field(default="pending", description="执行状态")


class PlanBudget(BaseModel):
    """研究预算上限。"""

    max_rounds: int = 2
    max_sources: int = 12
    max_tokens: int = 12000
    max_seconds: int = 45


class PlanDraft(BaseModel):
    """研究计划。"""

    objective: str = Field(description="一句话概括研究目标")
    sub_questions: list[str] = Field(description="1 个核心原问题 + 2~3 个扩展子问题")
    outline: list[OutlineSection] = Field(description="报告章节大纲")
    research_questions: list[str] = Field(default_factory=list, description="检索用问题清单")
    budget: PlanBudget = Field(default_factory=PlanBudget)


class Claim(BaseModel):
    """一条绑定来源的结论。"""

    claim_id: str = Field(description="结论标识，如 c_1")
    claim: str = Field(description="结论正文")
    confidence: Literal["high", "medium", "low"] = Field(default="medium")
    source_ids: list[str] = Field(default_factory=list, description="支撑该结论的来源 ID")


class ClaimMapEntry(BaseModel):
    """结论到来源的映射。"""

    claim_id: str
    source_ids: list[str] = Field(default_factory=list)


class AnalysisDraft(BaseModel):
    """证据分析与完备性评估。"""

    analysis_summary: str = Field(description="分析总结")
    needs_more_research: bool = Field(default=False, description="证据是否不足以回答问题")
    missing_gaps: list[str] = Field(default_factory=list, description="尚缺的信息")
    findings: list[Claim] = Field(default_factory=list, description="结论列表")
    claim_map: list[ClaimMapEntry] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class SupplementaryQuery(BaseModel):
    """一条补检索计划。"""

    section_id: str = Field(description="缺口标识，如 gap_1")
    query: str = Field(description="检索词")
    source_preference: Literal["hybrid", "web", "local"] = Field(default="hybrid")
    reason: str = Field(default="", description="为什么这条检索能填补缺口")


class ReflectionDraft(BaseModel):
    """补检索计划。"""

    reflection_summary: str = Field(description="补搜思路概述")
    supplementary_queries: list[SupplementaryQuery] = Field(default_factory=list)
