"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import json
import logging
import re

from ..state import AgentState

logger = logging.getLogger("mult_agents")


def _default_plan(state: AgentState) -> dict:
    return {
        "objective": state["query"],
        "sub_questions": [state["query"]],
        "outline": [
            {
                "id": "sec_1",
                "title": "默认大纲",
                "description": "默认生成的大纲",
                "section_type": "mixed",
                "requires_data": False,
                "requires_chart": False,
                "priority": 1,
                "search_queries": [state["query"]],
                "status": "pending",
            }
        ],
        "research_questions": [state["query"]],
        "budget": {"max_rounds": 2, "max_sources": 12, "max_tokens": 12000, "max_seconds": 45},
    }



# 句首指令性敬语/动词，剥离后避免被误当作检索实体（如「请调研」「帮我分析」）
_INSTRUCTION_PREFIXES = (
    "请帮我", "请帮", "麻烦帮我", "麻烦", "请", "帮我", "想要", "我想", "需要",
)
_INSTRUCTION_VERBS = (
    "调研", "分析", "梳理", "总结", "介绍", "解释", "研究", "评估",
    "对比", "比较", "盘点", "调查", "归纳", "整理", "阐述", "说明",
)
# 中文停用词（指令残留 + 泛化疑问词），不作为检索实体
_CN_STOPWORDS = {
    "帮我", "调查", "最新", "使用趋势", "是什么", "多少", "情况",
    "请调研", "调研", "请分析", "分析", "请梳理", "梳理", "请总结", "总结",
    "请介绍", "介绍", "请解释", "解释", "请研究", "研究", "请评估", "评估",
    "请对比", "对比", "请比较", "比较", "告诉我", "如何", "怎么", "为什么",
    "有哪些", "哪些", "能否", "一下",
}
# 动词后的语气助词（「介绍一下X」「分析一下X」），剥离后露出实体主体
_VERB_PARTICLES = ("一下",)


def _strip_instruction(query: str) -> str:
    """剥离句首指令性敬语与动词（「请」「帮我」「请调研」等），返回实体主体。"""
    stripped = query.strip()
    for prefix in _INSTRUCTION_PREFIXES:
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):].lstrip("，,：: 　")
            break
    for verb in _INSTRUCTION_VERBS:
        if stripped.startswith(verb):
            stripped = stripped[len(verb):].lstrip("，,：: 　")
            break
    for particle in _VERB_PARTICLES:
        if stripped.startswith(particle):
            stripped = stripped[len(particle):].lstrip("，,：: 　")
            break
    return stripped


def _guess_primary_entity(query: str) -> str:
    base = _strip_instruction(query)
    lowered = base.lower()
    ascii_terms = re.findall(r"[a-z][a-z0-9_-]{2,}", lowered)
    for term in ascii_terms:
        if term not in {"latest", "trend", "news", "agent", "open", "using"}:
            return term
    chinese_terms = re.findall(r"[\u4e00-\u9fff]{2,}", base)
    for term in chinese_terms:
        if term not in _CN_STOPWORDS:
            return term
    return ""



def _derive_direct_search_queries(query: str) -> list[str]:
    base_query = _strip_instruction(query.strip())
    if not base_query:
        return []
    entity = _guess_primary_entity(base_query)
    candidates = [base_query]
    if entity:
        candidates.extend(
            [
                f"{entity}是什么",
                f"{entity} GitHub",
                f"{entity} 官方文档",
                f"{entity} 使用趋势",
                f"{entity} AI Agent",
            ]
        )
    else:
        candidates.extend(
            [
                f"{base_query} 是什么",
                f"{base_query} GitHub",
                f"{base_query} 官方文档",
            ]
        )
    deduped: list[str] = []
    for item in candidates:
        text = item.strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped[:6]



def _is_query_grounded(candidate: str, user_query: str) -> bool:
    candidate_terms = set(_extract_query_terms(candidate))
    user_terms = set(_extract_query_terms(user_query))
    if not candidate_terms or not user_terms:
        return False
    if _guess_primary_entity(user_query) and _guess_primary_entity(user_query) in candidate.lower():
        return True
    overlap = candidate_terms & user_terms
    return len(overlap) >= 1



def _derive_search_plan(outline: list[dict], sub_questions: list[str], _research_questions: list[str], query: str) -> list[dict]:
    plan: list[dict] = []
    # 规划子问题优先：planner 拆解的检索意图比原始 query 更适合检索（已剥离疑问语气词）
    for sub_question in sub_questions or []:
        text = str(sub_question).strip().rstrip("？?。.!！")
        if text:
            plan.append(
                {
                    "section_id": "sub_question",
                    "query": text,
                    "source_preference": "hybrid",
                    "reason": "来自规划子问题",
                }
            )
    for direct_query in _derive_direct_search_queries(query):
        plan.append(
            {
                "section_id": "user_query",
                "query": direct_query,
                "source_preference": "hybrid",
                "reason": "围绕用户原始问题生成的直接检索词",
            }
        )
    for section in outline:
        if not isinstance(section, dict):
            continue
        section_id = str(section.get("id") or "sec")
        for item in section.get("search_queries", []) or []:
            text = str(item).strip()
            if text and _is_query_grounded(text, query):
                plan.append(
                    {
                        "section_id": section_id,
                        "query": text,
                        "source_preference": "hybrid",
                        "reason": f"来自大纲章节 {section_id}",
                    }
                )
    if not plan:
        plan.append({"section_id": "sec_1", "query": query, "source_preference": "hybrid", "reason": "fallback"})
    deduped = _dedupe_sources(plan, ["query"])
    return deduped[:6]



def _build_queries(state: AgentState, source_preference: str) -> list[dict]:
    queries: list[dict] = []
    
    # Check if we are in re-search iteration
    iteration = state.get("iteration", 0)
    if iteration > 0 and state.get("supplementary_queries"):
        base_plan = state.get("supplementary_queries", [])
    else:
        base_plan = state.get("search_plan", [])
        
    for item in base_plan:
        if not isinstance(item, dict):
            continue
        pref = item.get("source_preference", "hybrid")
        if pref in (source_preference, "hybrid"):
            query = str(item.get("query", "")).strip()
            if query:
                queries.append(item)
    if not queries:
        queries.append({"section_id": "sec_1", "query": state["query"], "source_preference": source_preference, "reason": "fallback"})
    return queries[:6]



def _extract_query_terms(query: str) -> list[str]:
    parts = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{3,}", query.lower())
    terms = []
    stopwords = {"什么", "如何", "以及", "一个", "关于", "这个", "那个", "进行", "基于", "附带", "来源", "清单"}
    for part in parts:
        if part in stopwords:
            continue
        terms.append(part)
    return terms[:12]



def _estimate_relevance(query: str, text: str) -> float:
    terms = _extract_query_terms(query)
    if not terms:
        return 0.0
    haystack = text.lower()
    hits = sum(1 for term in terms if term in haystack)
    return hits / max(len(terms), 1)



def _is_bad_web_domain(domain: str) -> bool:
    value = domain.lower()
    blocked = ["datasheet", "bdtic", "doc88", "elecfans", "down"]
    return any(item in value for item in blocked)



def _filter_web_records(query: str, records: list[dict]) -> tuple[list[dict], dict]:
    kept = []
    stats = {"raw_count": len(records), "kept_count": 0, "dropped_irrelevant": 0, "dropped_domain": 0, "dropped_empty": 0}
    for record in records:
        title = str(record.get("title", ""))
        snippet = str(record.get("snippet", ""))
        domain = str(record.get("domain", ""))
        if not title and not snippet:
            stats["dropped_empty"] += 1
            continue
        if _is_bad_web_domain(domain):
            stats["dropped_domain"] += 1
            continue
        relevance = _estimate_relevance(query, f"{title}\n{snippet}")
        record["relevance_score"] = relevance
        if relevance < 0.2 and not _is_official_domain(domain):
            stats["dropped_irrelevant"] += 1
            continue
        kept.append(record)
    stats["kept_count"] = len(kept)
    return kept, stats



def _filter_local_records(query: str, records: list[dict]) -> tuple[list[dict], dict]:
    kept = []
    stats = {"raw_count": len(records), "kept_count": 0, "dropped_irrelevant": 0, "dropped_missing_doc": 0, "dropped_empty": 0}
    for record in records:
        title = str(record.get("title", ""))
        snippet = str(record.get("snippet", ""))
        doc_id = str(record.get("doc_id", "")).strip()
        if not snippet:
            stats["dropped_empty"] += 1
            continue
        relevance = _estimate_relevance(query, f"{title}\n{snippet}")
        record["relevance_score"] = relevance
        if not doc_id and relevance < 0.35:
            stats["dropped_missing_doc"] += 1
            continue
        # 🔧 修复 #1：本地 RAG 相关性阈值从 0.2 → 0.35
        # 原 0.2 会导致完全无关的文档（如"DeepResearch架构"文档被"就业调研"查询召回）
        # 通过后端的硬过滤进入证据池，最终被 Writer 当作可靠来源成文
        if relevance < 0.35:
            stats["dropped_irrelevant"] += 1
            logger.info(
                "[local_rag] 过滤低相关文档 | relevance=%.2f | doc=%s | query_terms=%s",
                relevance, doc_id or title[:40], _extract_query_terms(query)[:5],
            )
            continue
        kept.append(record)
    stats["kept_count"] = len(kept)
    return kept, stats



def _format_raw_records(records: list[dict], source_type: str) -> str:
    if not records:
        return "[]"
    lines = []
    for record in records[:40]:
        locator = record.get("url") or record.get("doc_id") or ""
        lines.append(
            json.dumps(
                {
                    "source_id": record.get("source_id"),
                    "title": record.get("title"),
                    "url": record.get("url", ""),
                    "doc_id": record.get("doc_id", ""),
                    "snippet": str(record.get("snippet", ""))[:500],
                    "source_type": source_type,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)



def _minimal_record_filter(records: list[dict], required_any: list[str]) -> list[dict]:
    kept: list[dict] = []
    for record in records:
        if any(str(record.get(field, "")).strip() for field in required_any):
            kept.append(record)
    return kept



def _assign_source_ids(records: list[dict], prefix: str) -> list[dict]:
    assigned: list[dict] = []
    for index, record in enumerate(records, 1):
        item = dict(record)
        item["source_id"] = f"{prefix}-{index}"
        assigned.append(item)
    return assigned



def _enrich_evidence_from_raw(evidence: list[dict], raw_records: list[dict]) -> list[dict]:
    """从原始记录中补充 evidence 中可能丢失的 url、domain 等字段"""
    raw_lookup = {str(r.get("source_id", "")).strip(): r for r in raw_records if r.get("source_id")}
    enriched = []
    for ev in evidence:
        item = dict(ev)
        sid = str(item.get("source_id", "")).strip()
        raw = raw_lookup.get(sid, {})
        # 补充 url（如 LLM 没有保留）
        if not item.get("url") and raw.get("url"):
            item["url"] = raw["url"]
        # 补充 domain
        if not item.get("domain") and raw.get("domain"):
            item["domain"] = raw["domain"]
        # 补充 title（如 LLM 没有保留）
        if not item.get("title") and raw.get("title"):
            item["title"] = raw["title"]
        enriched.append(item)
    return enriched



def _prune_evidence_to_allowed_sources(evidence: list[dict], allowed_source_ids: set[str]) -> list[dict]:
    kept: list[dict] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id", "")).strip()
        if source_id and source_id in allowed_source_ids:
            kept.append(item)
    return kept



def _summarize_records(records: list[dict]) -> list[dict]:
    summary: list[dict] = []
    for record in records[:5]:
        summary.append(
            {
                "source_id": record.get("source_id"),
                "title": record.get("title", ""),
                "locator": record.get("url") or record.get("doc_id") or "",
                "snippet": str(record.get("snippet", ""))[:160],
            }
        )
    return summary



def _normalize_source_ids(values) -> list[str]:
    normalized: list[str] = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized



def _finalize_query_traces(query_traces: list[dict], kept_ids: set[str], rejected_ids: list[str], reject_reason: str) -> list[dict]:
    normalized_rejected = set(_normalize_source_ids(rejected_ids))
    finalized: list[dict] = []
    for trace in query_traces:
        raw_items = [item for item in trace.get("raw_records", []) if isinstance(item, dict)]
        kept_records = [item for item in raw_items if str(item.get("source_id", "")).strip() in kept_ids]
        rejected_records = [
            item
            for item in raw_items
            if str(item.get("source_id", "")).strip() in normalized_rejected or str(item.get("source_id", "")).strip() not in kept_ids
        ]
        trace_item = dict(trace)
        trace_item["raw_source_ids"] = _normalize_source_ids(item.get("source_id") for item in raw_items)
        trace_item["kept_source_ids"] = _normalize_source_ids(item.get("source_id") for item in kept_records)
        trace_item["rejected_source_ids"] = _normalize_source_ids(item.get("source_id") for item in rejected_records)
        trace_item["kept_count"] = len(trace_item["kept_source_ids"])
        trace_item["rejected_count"] = len(trace_item["rejected_source_ids"])
        trace_item["kept_records"] = kept_records[:3]
        trace_item["rejected_records"] = rejected_records[:3]
        if reject_reason:
            trace_item["reject_reason"] = reject_reason
        finalized.append(trace_item)
    return finalized



def _fallback_web_evidence(records: list[dict]) -> dict:
    evidence = []
    for record in records:
        evidence.append(
            {
                "source_id": record.get("source_id"),
                "title": record.get("title"),
                "url": record.get("url", ""),
                "snippet": record.get("snippet", ""),
                "domain": record.get("domain", ""),
                "source_type": "web",
                "reliability_hint": "official" if _is_official_domain(record.get("domain", "")) else "unknown",
                "supports": [],
                "notes": "",
            }
        )
    return {"summary": "完成网页证据采集。", "evidence": evidence, "gaps": []}



def _fallback_local_evidence(records: list[dict]) -> dict:
    evidence = []
    for record in records:
        evidence.append(
            {
                "source_id": record.get("source_id"),
                "doc_id": record.get("doc_id", ""),
                "title": record.get("title", "") or record.get("source_id", ""),
                "snippet": record.get("snippet", ""),
                "source_type": "local",
                "reliability_hint": "internal",
                "supports": [],
                "notes": "",
            }
        )
    return {"summary": "完成本地知识库证据采集。", "evidence": evidence, "gaps": []}



def _is_official_domain(domain: str) -> bool:
    value = domain.lower()
    return value.endswith(".gov.cn") or value.endswith(".gov") or value.endswith(".edu") or value.endswith(".edu.cn") or "gov" in value or "official" in value



def _score_evidence(record: dict) -> tuple[float, str]:
    source_type = record.get("source_type")
    if source_type == "local":
        return 0.92, "企业内部知识库证据，默认高可信"
    domain = str(record.get("domain", "")).lower()
    if _is_official_domain(domain):
        return 0.88, "官方或权威机构域名"
    if any(word in domain for word in ["news", "finance", "reuters", "bloomberg", "people", "xinhuanet"]):
        return 0.72, "主流媒体域名"
    if domain:
        return 0.58, "普通互联网来源，需要交叉验证"
    return 0.45, "来源信息不完整"


_EVIDENCE_SCORE_PROMPT = """你是一名证据质量评审专家。请针对研究问题，评估每条证据的内容质量与主题相关性。

研究问题：{query}

证据列表：
{evidence_list}

要求：
1. 对每条证据输出 0~1 之间的相关性/可靠性分数（0 完全不可信或无关，1 高度可信且高度相关）
2. 分数参考维度：内容与问题的相关性、信息具体性、是否存在明显偏见或营销倾向、时效性
3. 只输出 JSON 数组，不要输出任何其他文字

输出格式（每条一个对象）：
[{{"source_id": "证据ID", "score": 0.85, "reason": "不超过30字的中文理由"}}]
"""


class EvidenceScorer:
    """证据评分融合：域名信誉先验 + LLM 结构化评估。

    LLM 批量评估一批证据（≤20 条），输出与先验加权融合。
    任何失败路径都静默回退纯先验，保证主流程零感知。
    """

    BATCH_SIZE = 20
    LLM_WEIGHT = 0.6

    def __init__(self, llm, prior_weight: float = 0.4):
        self._llm = llm
        self._prior_weight = prior_weight
        self._llm_weight = 1.0 - prior_weight

    def score_batch(self, records: list[dict]) -> list[dict]:
        """批量评分入口：返回带 reliability_score / reliability_reason 的新记录列表。"""
        if not records:
            return []
        scored = []
        for start in range(0, len(records), self.BATCH_SIZE):
            batch = records[start: start + self.BATCH_SIZE]
            llm_scores = self._llm_score_batch(batch)
            for record in batch:
                prior, prior_reason = _score_evidence(record)
                sid = record.get("source_id", "")
                llm_result = llm_scores.get(sid)
                if llm_result is None:
                    score, reason = prior, prior_reason
                else:
                    raw = self._prior_weight * prior + self._llm_weight * llm_result["score"]
                    score = min(1.0, max(0.0, raw))
                    reason = f"{llm_result['reason']}（先验{prior:.2f}）"
                item = dict(record)
                item["reliability_score"] = score
                item["reliability_reason"] = reason
                scored.append(item)
        return scored

    def _llm_score_batch(self, batch: list[dict]) -> dict:
        """调用 LLM 批量评估，返回 {source_id: {score, reason}}；失败返回 {}。"""
        lines = []
        for r in batch:
            sid = r.get("source_id", "")
            title = r.get("title", "")
            locator = r.get("domain") or r.get("doc_id") or ""
            snippet = str(r.get("snippet", ""))[:200]
            lines.append(f"[{sid}] {title} | {locator} | {snippet}")
        evidence_list = "\n".join(lines)
        query = batch[0].get("query", "") if batch else ""
        prompt = _EVIDENCE_SCORE_PROMPT.format(query=query, evidence_list=evidence_list)
        try:
            resp = self._llm.invoke(prompt)
            content = resp.content if hasattr(resp, "content") else str(resp)
        except Exception as exc:
            logger.warning("[evidence_scorer] LLM 调用失败，整批回退先验 | %s", exc)
            return {}
        parsed = self._parse_llm_json(content)
        if not isinstance(parsed, list):
            return {}
        result = {}
        valid_sids = {r.get("source_id", "") for r in batch}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("source_id", "")).strip()
            if sid not in valid_sids:
                continue
            try:
                score = float(item.get("score", 0))
            except (TypeError, ValueError):
                continue
            score = min(1.0, max(0.0, score))
            reason = str(item.get("reason", ""))[:50]
            result[sid] = {"score": score, "reason": reason}
        return result

    @staticmethod
    def _parse_llm_json(content: str):
        """<arg_value>解析 LLM 输出：先直接 json.loads，失败用正则截取首个 [...] 子串再解析。
        """
        text = content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass
        match = re.search(r"\[.*?\]", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except (json.JSONDecodeError, TypeError):
                pass
        return None



def _dedupe_sources(items: list[dict], key_fields: list[str]) -> list[dict]:
    seen = set()
    results = []
    for item in items:
        key = tuple(str(item.get(field, "")).strip() for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        results.append(item)
    return results



