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
    # 上限放宽到 500：实测 qwen3.7-flash 常输出 200 字以上的理由，过严的上限
    # 会让整次结构化调用因一个审计字段失败；description 负责引导简短。
    reason: str = Field(default="", max_length=500, description="判断理由，简要说明，百字以内")


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


class SupplementaryQuery(BaseModel):
    """一条补检索计划。"""

    section_id: str = Field(description="缺口标识，如 gap_1")
    query: str = Field(description="检索词")
    source_preference: Literal["hybrid", "web", "local"] = Field(default="hybrid")
    reason: str = Field(default="", description="为什么这条检索能填补缺口")


class AnalysisDraft(BaseModel):
    """证据分析与完备性评估。

    同时承担「缺口 → 补检查询」的生成（原 `reflect` 节点的职责）。
    合并的理由：缺口与补检词本就出自同一次判断，分两次调用会让模型
    先说出缺口、再被要求根据自己刚写的缺口出词 —— 多一次调用，还多一次信息损失。
    """

    analysis_summary: str = Field(description="分析总结")
    needs_more_research: bool = Field(default=False, description="证据是否不足以回答问题")
    missing_gaps: list[str] = Field(default_factory=list, description="尚缺的信息")
    gap_queries: list[SupplementaryQuery] = Field(
        default_factory=list,
        description="针对 missing_gaps 的补检索词；needs_more_research 为假时留空",
    )
    findings: list[Claim] = Field(default_factory=list, description="结论列表")
    claim_map: list[ClaimMapEntry] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)


class RetrievalGradeDraft(BaseModel):
    """检索充分性判定：内层自适应循环的裁判。

    层级说明：本节点判「这一轮检索够不够，不够就换个词再搜」，
    是**检索阶段内部**的循环；「跨轮次还要不要继续研究」由 `analyze` 的
    `next_action` 决定。两者层级不同，不要混用。
    """

    sufficient: bool = Field(description="现有证据是否足以支撑后续分析")
    gaps: list[str] = Field(default_factory=list, description="仍缺的信息；sufficient 为真时留空")
    rewritten_queries: list[SupplementaryQuery] = Field(
        default_factory=list, description="针对缺口的新检索词；sufficient 为真时留空"
    )


# ── 检索节点：证据整理的结构化产出 ──
#
# 这几个 schema 只约束「结构必需字段」（source_id 必须存在且非空），
# 其余一律给默认值 —— 证据正文由模型自由生成，字段缺失不该让整轮检索失败。


class WebEvidenceItem(BaseModel):
    """网页证据条目。"""

    source_id: str = Field(description="来源 ID，必须取自输入，不得编造")
    title: str = ""
    url: str = ""
    snippet: str = ""
    domain: str = ""
    source_type: str = "web"
    reliability_hint: str = Field(default="unknown", description="official / media / community / unknown")
    supports_questions: list[str] = Field(default_factory=list)
    notes: str = ""


class WebSearchDraft(BaseModel):
    """WebScout 的相关性过滤结果。"""

    summary: str = ""
    evidence: list[WebEvidenceItem] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    rejected_source_ids: list[str] = Field(default_factory=list)
    reject_reason: str = ""


class LocalEvidenceItem(BaseModel):
    """本地知识库证据条目。"""

    source_id: str = Field(description="来源 ID，必须取自输入，不得编造")
    doc_id: str = ""
    title: str = ""
    snippet: str = ""
    source_type: str = "local"
    reliability_hint: str = "internal"
    supports_questions: list[str] = Field(default_factory=list)
    notes: str = ""


class LocalRagDraft(BaseModel):
    """LocalRAGScout 的相关性过滤结果。"""

    summary: str = ""
    evidence: list[LocalEvidenceItem] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    rejected_source_ids: list[str] = Field(default_factory=list)
    reject_reason: str = ""


class EvidencePoolItem(BaseModel):
    """证据裁判后的证据条目。"""

    source_id: str = Field(description="来源 ID，必须取自输入，不得编造")
    source_type: str = ""
    title: str = ""
    url: str = ""
    doc_id: str = ""
    snippet: str = ""
    supports_questions: list[str] = Field(default_factory=list)
    reliability_score: float = 0.5
    reliability_reason: str = ""
    source_label: str = ""


class AuditFlag(BaseModel):
    """证据审计标记。"""

    type: str = Field(default="missing_evidence", description="low_confidence / conflict / missing_evidence")
    target: str = ""
    reason: str = ""


class DeepDiveDraft(BaseModel):
    """EvidenceJudge 的证据裁判结果。"""

    summary: str = ""
    evidence_pool: list[EvidencePoolItem] = Field(default_factory=list)
    audit_flags: list[AuditFlag] = Field(default_factory=list)
