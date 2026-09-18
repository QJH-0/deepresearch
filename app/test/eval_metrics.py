"""
DeepResearch 多智能体深度研报助手 — 自动化评测脚本

测量 7 项指标：
  1. 研究完备性（LLM-as-Judge + key_points）
  2. 检索耗时降低比例（系统埋点计时）
  3. 低质信源过滤率（_score_evidence 函数自动统计）
  4. 幻觉率（LLM-as-Judge 事实核查）
  5. 引用准确率（规则校验 + LLM-as-Judge 语义匹配）
  6. Token 消耗降低比例（DashScope API usage 统计）
  7. 简单问答响应时间（系统埋点计时）

运行方式:
  cd D:\\Code\\LLMdev\\deepresearch
  python app/test/eval_metrics.py --output output/eval_report.json [--max-queries N] [--judge-model M]

前置：Postgres / Milvus / Redis / RabbitMQ 已启动，.env 有 DASHSCOPE_API_KEY。
注意 `--config` 参数已废弃（配置统一由 AppConfig.from_file() 从 .env + config.json 读取）。
"""

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
# 必须同时插入 app/：mult_agents 是 app/ 下的顶层包，只插项目根会
# ModuleNotFoundError: No module named 'mult_agents'
for _path in (_PROJECT_ROOT, _APP_PATH):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from langchain_core.callbacks import BaseCallbackHandler

from mult_agents.config import AppConfig
from mult_agents.graph import build_app as build_workflow_app
from mult_agents.runtime import init_checkpointer, close_checkpointer
from mult_agents.models import build_agents
from mult_agents.state import create_initial_state
from mult_agents.eval_metrics import measure as measure_quality, aggregate as aggregate_metric

logger = logging.getLogger("eval")


# ======================================================================
# 测试集
# ======================================================================

GOLDEN_SET_PATH = Path(__file__).resolve().parent / "golden_set.json"


def load_golden_set(path: Path | None = None) -> list[dict]:
    """从 golden_set.json 读评测题集。

    题集是**数据**不是代码：标注「期望要点」「期望来源」需要反复编辑与评审，
    放在 JSON 里比改 Python 字面量安全得多，也便于多人协作与 diff 审阅。
    要点判别力的要求写在该文件的 _readme 字段里，改题前先读。
    """
    target = path or GOLDEN_SET_PATH
    with open(target, encoding="utf-8") as handle:
        document = json.load(handle)
    queries = document.get("queries") or []
    if not queries:
        raise ValueError(f"评测题集为空或格式不对: {target}")
    return queries


# ======================================================================
# Token 计数 Hook
# ======================================================================

class TokenAccumulator(BaseCallbackHandler):
    """按 LangChain 回调累计 token 用量。

    旧实现把钩子挂在 agent 对象上（`agent._generate`），但结构化节点是
    `StructuredAgent`（内含 create_agent 编译出的图），**没有 `_generate`**，
    于是 `hasattr(agent, "_generate")` 恒为假、一次都没挂上，
    token 统计恒为 0 且不报错——报告里的「Token 消耗降低比例」实际是假数据。

    改为回调后由 invoke 的 `config["callbacks"]` 注入，能覆盖图内所有嵌套 LLM 调用。
    解析不到 usage 时计数并告警，避免再次静默归零。
    """

    def __init__(self):
        super().__init__()
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.call_count = 0
        self.unparsed_calls = 0

    @staticmethod
    def _usage_from(response) -> dict | None:
        """从 LLMResult 提取 usage，兼容 llm_output 与 usage_metadata 两种落点。"""
        llm_output = getattr(response, "llm_output", None) or {}
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        if isinstance(usage, dict) and usage:
            return usage

        for generations in getattr(response, "generations", None) or []:
            for generation in generations:
                metadata = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if isinstance(metadata, dict) and metadata:
                    return {
                        "prompt_tokens": metadata.get("input_tokens", 0),
                        "completion_tokens": metadata.get("output_tokens", 0),
                    }
        return None

    def on_llm_end(self, response, **kwargs):
        usage = self._usage_from(response)
        if usage is None:
            self.unparsed_calls += 1
            return
        self.total_prompt_tokens += int(usage.get("prompt_tokens", 0) or 0)
        self.total_completion_tokens += int(usage.get("completion_tokens", 0) or 0)
        self.call_count += 1

    def reset(self):
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.call_count = 0
        self.unparsed_calls = 0

    def warn_if_unparsed(self):
        """解析不到 usage 时必须留痕，否则又变成「静默归零」。"""
        if self.unparsed_calls and not self.call_count:
            logger.warning(
                "token 统计未取到任何 usage（%d 次调用无法解析）——本次报告中的 token 指标不可信",
                self.unparsed_calls,
            )

    @property
    def total_tokens(self):
        return self.total_prompt_tokens + self.total_completion_tokens


# ======================================================================
# LLM-as-Judge
# ======================================================================

def build_judge_llm(api_key: str, model: str, *, timeout: float = 60.0, max_retries: int = 2):
    """评测用裁判模型。

    与主链路共用 ChatOpenAI 兼容通道（`build_aux_llm`）：原生 SDK 通道不支持
    Qwen3.7/3.8 系列，继续用它会把评测锁死在旧型号上，且与生产链路行为不一致。
    """
    from mult_agents.models import build_aux_llm

    return build_aux_llm(model, api_key, timeout=timeout, max_retries=max_retries, temperature=0.0)


def _extract_json(text: str) -> dict:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(cleaned[start:end + 1])
    return {}


def judge_completeness(judge_llm, query: str, key_points: list, report: str) -> dict:
    from langchain_core.messages import HumanMessage
    prompt = (
        f"你是研报质量评审员。请判断以下研报内容是否覆盖了给定的核心论点。\n\n"
        f"【调研问题】：{query}\n"
        f"【应覆盖的核心论点】：\n"
        f"{chr(10).join(f'- {p}' for p in key_points)}\n"
        f"【研报内容】：\n{report[:3000]}\n\n"
        f"请对每个核心论点逐条判定：covered / partial / missing。\n"
        f"只输出JSON：\n"
        f'{{"results": [{{"point": "...", "verdict": "covered|partial|missing", "evidence": "..."}}]}}'
    )
    try:
        resp = judge_llm.invoke([HumanMessage(content=prompt)])
        data = _extract_json(resp.content)
        results = data.get("results", [])
        covered = sum(1 for r in results if r.get("verdict") == "covered")
        partial = sum(1 for r in results if r.get("verdict") == "partial")
        missing = sum(1 for r in results if r.get("verdict") == "missing")
        total = len(key_points)
        score = (covered + 0.5 * partial) / total if total > 0 else 0
        return {"score": round(score, 4), "covered": covered, "partial": partial, "missing": missing, "total": total}
    except Exception as e:
        logger.warning("Judge completeness failed: %s", e)
        return {"score": 0, "covered": 0, "partial": 0, "missing": len(key_points), "total": len(key_points)}


def judge_hallucination(judge_llm, report: str, evidence_pool: list) -> dict:
    from langchain_core.messages import HumanMessage
    sentences = [s.strip() for s in re.split(r'[。！？\n]', report) if len(s.strip()) > 15]
    if not sentences:
        return {"hallucination_rate": 0, "total_sentences": 0, "hallucinated": 0}
    sentences = sentences[:30]
    snippets_text = "\n".join(
        f"- [{e.get('source_id', '?')}]: {str(e.get('snippet', ''))[:200]}"
        for e in evidence_pool[:20]
    )
    prompt = (
        f"你是事实核查员。请逐句判断研报中的事实性陈述是否有来源支撑。\n\n"
        f"【研报陈述列表】：\n{chr(10).join(f'{i+1}. {s}' for i, s in enumerate(sentences))}\n\n"
        f"【可用来源摘要】：\n{snippets_text}\n\n"
        f"请对每条陈述判定：grounded / hallucinated。\n"
        f"只输出JSON：\n"
        f'{{"results": [{{"sentence_id": 1, "verdict": "grounded|hallucinated", "reason": "..."}}]}}'
    )
    try:
        resp = judge_llm.invoke([HumanMessage(content=prompt)])
        data = _extract_json(resp.content)
        results = data.get("results", [])
        hallucinated = sum(1 for r in results if r.get("verdict") == "hallucinated")
        total = len(sentences)
        return {"hallucination_rate": round(hallucinated / total, 4) if total > 0 else 0,
                "total_sentences": total, "hallucinated": hallucinated}
    except Exception as e:
        logger.warning("Judge hallucination failed: %s", e)
        return {"hallucination_rate": 0, "total_sentences": len(sentences), "hallucinated": 0}


def judge_citation_accuracy(judge_llm, report: str, source_index: list) -> dict:
    from langchain_core.messages import HumanMessage
    citation_pattern = r'\[([A-Z]+\d+_\d+-\d+)\]'
    found_citations = list(dict.fromkeys(re.findall(citation_pattern, report)))
    if not found_citations:
        return {"accuracy_rate": 0, "total_citations": 0, "accurate": 0, "legality_rate": 0}

    source_lookup = {}
    for s in source_index:
        sid = s.get("source_id", "")
        if sid:
            source_lookup[sid] = (s.get("label", "") or "") + " " + str(s.get("locator", ""))

    valid_citations = [c for c in found_citations if c in source_lookup]
    legality_rate = len(valid_citations) / len(found_citations) if found_citations else 0
    if not valid_citations:
        return {"accuracy_rate": 0, "total_citations": len(found_citations), "accurate": 0, "legality_rate": round(legality_rate, 4)}

    citations_text = "\n".join(f"[{c}]: {source_lookup.get(c, '未知')[:200]}" for c in valid_citations[:20])
    report_paragraphs = [p for p in re.split(r'\n', report) if '[' in p][:15]
    prompt = (
        f"你是引用审核员。请判断每个引用标记所指向的来源内容是否与正文陈述语义匹配。\n\n"
        f"【正文段落（含引用标记）】：\n{chr(10).join(report_paragraphs)}\n\n"
        f"【引用来源对应表】：\n{citations_text}\n\n"
        f"请对每个引用判定：accurate / mismatch。\n"
        f"只输出JSON：\n"
        f'{{"results": [{{"citation_id": "...", "verdict": "accurate|mismatch", "reason": "..."}}]}}'
    )
    try:
        resp = judge_llm.invoke([HumanMessage(content=prompt)])
        data = _extract_json(resp.content)
        results = data.get("results", [])
        accurate = sum(1 for r in results if r.get("verdict") == "accurate")
        total = len(valid_citations)
        return {"accuracy_rate": round(accurate / total, 4) if total > 0 else 0,
                "total_citations": total, "accurate": accurate, "legality_rate": round(legality_rate, 4)}
    except Exception as e:
        logger.warning("Judge citation accuracy failed: %s", e)
        return {"accuracy_rate": 0, "total_citations": len(valid_citations), "accurate": 0, "legality_rate": round(legality_rate, 4)}


def judge_with_consensus(judge_func, judge_llm, *args, rounds=3):
    scores = []
    results = []
    for _ in range(rounds):
        r = judge_func(judge_llm, *args)
        results.append(r)
        s = r.get("score", r.get("hallucination_rate", r.get("accuracy_rate", 0)))
        scores.append(s)
    scores_sorted = sorted(scores)
    median = scores_sorted[len(scores_sorted) // 2]
    for r in results:
        s = r.get("score", r.get("hallucination_rate", r.get("accuracy_rate", 0)))
        if s == median:
            return r
    return results[0]


# ======================================================================
# 系统埋点统计
# ======================================================================

def extract_retrieval_stats(final_state: dict) -> dict:
    web_stats = final_state.get("web_retrieval_stats", {})
    local_stats = final_state.get("local_retrieval_stats", {})
    evidence_pool = final_state.get("evidence_pool", [])
    low_quality_count = sum(1 for e in evidence_pool if float(e.get("reliability_score", 1.0)) < 0.6)
    total_evidence = len(evidence_pool)
    return {
        "web_query_count": web_stats.get("query_count", 0),
        "web_raw_count": web_stats.get("raw_count", 0),
        "web_kept_count": web_stats.get("kept_count", 0),
        "web_dropped_count": web_stats.get("dropped_count", 0),
        "local_query_count": local_stats.get("query_count", 0),
        "local_raw_count": local_stats.get("raw_count", 0),
        "local_kept_count": local_stats.get("kept_count", 0),
        "local_dropped_count": local_stats.get("dropped_count", 0),
        "total_evidence": total_evidence,
        "low_quality_count": low_quality_count,
        "low_quality_rate": round(low_quality_count / total_evidence, 4) if total_evidence > 0 else 0,
    }


# ======================================================================
# 评测数据结构
# ======================================================================

@dataclass
class EvalResult:
    query: str
    query_type: str
    elapsed_time: float
    final_output: str
    token_count: int
    retrieval_stats: dict
    completeness: dict = field(default_factory=dict)
    hallucination: dict = field(default_factory=dict)
    citation_accuracy: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)
    intent: str = ""
    error: str = ""


def _result_to_dict(r: EvalResult) -> dict:
    return {
        "query": r.query, "type": r.query_type, "elapsed_time": round(r.elapsed_time, 2),
        "token_count": r.token_count, "retrieval_stats": r.retrieval_stats,
        "completeness": r.completeness, "hallucination": r.hallucination,
        "citation_accuracy": r.citation_accuracy, "quality": r.quality,
        "intent": r.intent,
        "error": r.error, "final_preview": r.final_output[:500],
    }


async def run_single_query(app, config, query, token_acc, memory_manager=None):
    # P5: 旧记忆系统已删除，新记忆走 MemoryService (langmem + PostgresStore)
    memory_context = ""
    state = create_initial_state(
        query=query, max_iterations=config.max_iterations,
        user_id=config.user_id, tenant_id=config.tenant_id, memory_context=memory_context)
    token_acc.reset()
    start = time.time()
    # callbacks 走 invoke 的 config 注入，能覆盖图内所有嵌套 LLM 调用
    cfg = {
        "configurable": {"thread_id": f"{config.thread_id}_eval_{int(start)}"},
        "callbacks": [token_acc],
    }
    # 节点全是 async 函数：同步 invoke 会抛
    # TypeError: No synchronous function provided to "intent"
    result = await app.ainvoke(state, cfg)
    elapsed = time.time() - start
    final = result.get("final", "")
    return final, dict(result), elapsed, token_acc.total_tokens


async def run_eval(output_path, max_queries=0, judge_model="", judge_rounds=3):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    config = AppConfig.from_file()
    agents = build_agents(config.model, config.api_key, config)
    # 必须用异步 checkpointer：sync PostgresSaver 没有 aget_tuple/aput，
    # ainvoke 会落到基类 stub 抛 NotImplementedError
    checkpointer = await init_checkpointer(config)
    app = build_workflow_app(agents, checkpointer)
    judge_llm = build_judge_llm(
        config.api_key,
        judge_model or config.model,
        timeout=config.llm_timeout_seconds,
        max_retries=config.llm_max_retries,
    )

    token_acc = TokenAccumulator()

    def judge(judge_func, *args):
        """按配置轮数取中位数。轮数是成本旋钮：3 轮最稳，跑大样本时降到 1 轮。"""
        return judge_with_consensus(judge_func, judge_llm, *args, rounds=judge_rounds)

    all_queries = load_golden_set()
    queries = all_queries[:max_queries] if max_queries > 0 else all_queries
    ma_queries = [q for q in queries if q["type"] == "multiagent"]
    dir_queries = [q for q in queries if q["type"] == "direct"]

    results_bl, results_im, results_dir = [], [], []

    # 三组配置统一关闭 HITL：config.json 默认开启，会让图在 plan/analyze/write 处
    # interrupt 并返回，评测拿到的 final 是空的。评测跑的是无人值守路径。
    # Baseline
    logger.info("=" * 60)
    logger.info("Phase 1: Baseline (max_iterations=1)")
    logger.info("=" * 60)
    bl_config = config.with_overrides(max_iterations=1, enable_memory=False, hitl_enabled=False)
    for i, q in enumerate(ma_queries):
        logger.info("[%d/%d] %s", i + 1, len(ma_queries), q["query"][:50])
        try:
            final, state, elapsed, tokens = await run_single_query(app, bl_config, q["query"], token_acc)
            stats = extract_retrieval_stats(state)
            quality = measure_quality(state, final, q["key_points"], q.get("expected_sources"))
            comp = judge(judge_completeness, q["query"], q["key_points"], final)
            halluc = judge(judge_hallucination, final, state.get("evidence_pool", []))
            cit = judge(judge_citation_accuracy, final, state.get("source_index", []))
            results_bl.append(EvalResult(q["query"], "multiagent", elapsed, final, tokens, stats,
                                        comp, halluc, cit, quality))
            logger.info("  完备=%.2f 幻觉=%.2f 引用=%.2f Token=%d 耗时=%.1fs | 重复=%.1f%% 检索轮次=%.1f",
                        comp.get("score", 0), halluc.get("hallucination_rate", 0), cit.get("accuracy_rate", 0),
                        tokens, elapsed, quality["evidence_duplication_rate"]["overall"] * 100,
                        quality["retrieval_rounds"]["queries_per_round"])
        except Exception as e:
            logger.error("  失败: %s", e)
            results_bl.append(EvalResult(q["query"], "multiagent", 0, "", 0, {}, error=str(e)))

    # Improved
    logger.info("=" * 60)
    logger.info("Phase 2: Improved (max_iterations=2)")
    logger.info("=" * 60)
    im_config = config.with_overrides(max_iterations=2, enable_memory=False, hitl_enabled=False)
    for i, q in enumerate(ma_queries):
        logger.info("[%d/%d] %s", i + 1, len(ma_queries), q["query"][:50])
        try:
            final, state, elapsed, tokens = await run_single_query(app, im_config, q["query"], token_acc)
            stats = extract_retrieval_stats(state)
            quality = measure_quality(state, final, q["key_points"], q.get("expected_sources"))
            comp = judge(judge_completeness, q["query"], q["key_points"], final)
            halluc = judge(judge_hallucination, final, state.get("evidence_pool", []))
            cit = judge(judge_citation_accuracy, final, state.get("source_index", []))
            results_im.append(EvalResult(q["query"], "multiagent", elapsed, final, tokens, stats,
                                        comp, halluc, cit, quality))
            logger.info("  完备=%.2f 幻觉=%.2f 引用=%.2f Token=%d 耗时=%.1fs | 重复=%.1f%% 检索轮次=%.1f",
                        comp.get("score", 0), halluc.get("hallucination_rate", 0), cit.get("accuracy_rate", 0),
                        tokens, elapsed, quality["evidence_duplication_rate"]["overall"] * 100,
                        quality["retrieval_rounds"]["queries_per_round"])
        except Exception as e:
            logger.error("  失败: %s", e)
            results_im.append(EvalResult(q["query"], "multiagent", 0, "", 0, {}, error=str(e)))

    # Direct
    logger.info("=" * 60)
    logger.info("Phase 3: Direct (简单问答)")
    logger.info("=" * 60)
    dir_config = config.with_overrides(max_iterations=1, enable_memory=False, hitl_enabled=False)
    for i, q in enumerate(dir_queries):
        logger.info("[%d/%d] %s", i + 1, len(dir_queries), q["query"][:50])
        try:
            final, state, elapsed, tokens = await run_single_query(app, dir_config, q["query"], token_acc)
            route = state.get("intent", "unknown")
            results_dir.append(EvalResult(q["query"], "direct", elapsed, final, tokens, {}, intent=route))
            logger.info("  路由=%s 耗时=%.2fs Token=%d", route, elapsed, tokens)
        except Exception as e:
            logger.error("  失败: %s", e)
            results_dir.append(EvalResult(q["query"], "direct", 0, "", 0, {}, error=str(e)))

    # 汇总
    def avg(lst, attr, key=None):
        vals = []
        for r in lst:
            if key:
                d = getattr(r, attr, {}) or {}
                if key in d:
                    vals.append(d[key])
            else:
                v = getattr(r, attr, 0)
                if v and v > 0:
                    vals.append(v)
        return sum(vals) / len(vals) if vals else 0

    bl_comp = avg(results_bl, "completeness", "score")
    im_comp = avg(results_im, "completeness", "score")
    bl_halluc = avg(results_bl, "hallucination", "hallucination_rate")
    im_halluc = avg(results_im, "hallucination", "hallucination_rate")
    im_cit = avg(results_im, "citation_accuracy", "accuracy_rate")
    bl_lq = avg(results_bl, "retrieval_stats", "low_quality_rate")
    im_lq = avg(results_im, "retrieval_stats", "low_quality_rate")
    bl_tok = avg(results_bl, "token_count")
    im_tok = avg(results_im, "token_count")
    bl_time = avg(results_bl, "elapsed_time")
    im_time = avg(results_im, "elapsed_time")
    dir_times = [r.elapsed_time for r in results_dir if r.elapsed_time > 0]
    dir_avg = sum(dir_times) / len(dir_times) if dir_times else 0
    dir_max = max(dir_times) if dir_times else 0

    # 规则型指标：零外部依赖，改代码后可立刻对比是否引入退化
    bl_quality = [r.quality for r in results_bl if r.quality]
    im_quality = [r.quality for r in results_im if r.quality]
    bl_dup = aggregate_metric(bl_quality, "evidence_duplication_rate", "overall")
    im_dup = aggregate_metric(im_quality, "evidence_duplication_rate", "overall")
    bl_rounds = aggregate_metric(bl_quality, "retrieval_rounds", "queries_per_round")
    im_rounds = aggregate_metric(im_quality, "retrieval_rounds", "queries_per_round")
    im_cit_legal = aggregate_metric(im_quality, "citation_legality", "legality_rate")
    im_kp = aggregate_metric(im_quality, "key_point_coverage", "coverage")
    # 未标注 expected_sources 的题 recall 为 None，aggregate 会自动跳过
    bl_src = aggregate_metric(bl_quality, "expected_source_recall", "recall")
    im_src = aggregate_metric(im_quality, "expected_source_recall", "recall")
    src_annotated = sum(
        1 for entry in im_quality if entry.get("expected_source_recall", {}).get("applicable")
    )

    report = {
        "summary": {
            "completeness": {"baseline": round(bl_comp, 4), "improved": round(im_comp, 4),
                             "delta": round(im_comp - bl_comp, 4),
                             "desc": "研究完备性（LLM-as-Judge + key_points 覆盖率，3轮取中位数）"},
            "retrieval_time": {"baseline_avg_s": round(bl_time, 2), "improved_avg_s": round(im_time, 2),
                               "reduction": round((bl_time - im_time) / bl_time, 4) if bl_time > 0 else 0,
                               "desc": "端到端耗时（系统埋点 time.time 计时，整题而非检索子阶段）"},
            "low_quality_rate": {"baseline": round(bl_lq, 4), "improved": round(im_lq, 4),
                                 "desc": "低质信源占比（_score_evidence < 0.6 的证据比例）"},
            "hallucination_rate": {"baseline": round(bl_halluc, 4), "improved": round(im_halluc, 4),
                                   "desc": "幻觉率（LLM-as-Judge 事实核查，3轮取中位数）"},
            "citation_accuracy": {"improved": round(im_cit, 4),
                                  "desc": "引用准确率（规则校验 + LLM-as-Judge 语义匹配）"},
            "token_reduction": {"baseline_avg": int(bl_tok), "improved_avg": int(im_tok),
                                "reduction": round((bl_tok - im_tok) / bl_tok, 4) if bl_tok > 0 else 0,
                                "desc": "Token 消耗降低（DashScope API usage hook 统计）"},
            "direct_response": {"avg_s": round(dir_avg, 2), "max_s": round(dir_max, 2),
                                "under_2s": dir_max < 2.0,
                                "desc": "简单问答响应时间（系统埋点计时）"},
            "evidence_duplication": {"baseline": round(bl_dup, 4), "improved": round(im_dup, 4),
                                     "desc": "证据重复率（同一 source_id 重复出现的比例；规则型，reducer 语义哨兵）"},
            "retrieval_depth": {"baseline_queries_per_round": round(bl_rounds, 2),
                                "improved_queries_per_round": round(im_rounds, 2),
                                "desc": "平均每轮检索查询数（规则型；自适应检索生效后应上升）"},
            "citation_legality": {"improved": round(im_cit_legal, 4),
                                  "desc": "引用角标合法率（规则型；角标能否在来源表找到）"},
            "key_point_coverage": {"improved": round(im_kp, 4),
                                   "desc": "期望要点字面覆盖率（规则型，对照 LLM-as-Judge）"},
            "expected_source_recall": {"baseline": round(bl_src, 4), "improved": round(im_src, 4),
                                       "annotated_queries": src_annotated,
                                       "desc": "期望来源召回率（规则型；仅统计已标注 expected_sources 的题，"
                                               "未标注时聚合自动跳过）"},
        },
        "details": {
            "baseline": [_result_to_dict(r) for r in results_bl],
            "improved": [_result_to_dict(r) for r in results_im],
            "direct": [_result_to_dict(r) for r in results_dir],
        },
        "meta": {
            "total_queries": len(all_queries),
            "multiagent_queries": len(ma_queries),
            "direct_queries": len(dir_queries),
            "judge_model": judge_model or config.model,
            "judge_rounds": judge_rounds,
            "gen_model": config.model,
        },
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info("评测报告已保存: %s", output_path)
    logger.info("=" * 60)
    print_summary(report)
    token_acc.warn_if_unparsed()
    await close_checkpointer()


def _delta_text(reduction: float) -> str:
    """把「降低比例」渲染成正负号明确的文案，避免负值被读成降低。"""
    if reduction >= 0:
        return f"降低 {reduction:.1%}"
    return f"**增加 {-reduction:.1%}**"


def print_summary(report):
    s = report["summary"]
    meta = report.get("meta", {})
    judge_model = meta.get("judge_model", "未知")
    print("\n" + "=" * 60)
    print("DeepResearch 自动化评测报告汇总")
    print("=" * 60)
    print(f"\n1. 研究完备性: {s['completeness']['baseline']:.1%} → {s['completeness']['improved']:.1%} (Δ={s['completeness']['delta']:+.1%})")
    print(f"   方法: LLM-as-Judge ({judge_model}) + key_points 覆盖率, {meta.get('judge_rounds', 3)}轮取中位数")
    print(f"\n2. 端到端耗时: {s['retrieval_time']['baseline_avg_s']:.1f}s → {s['retrieval_time']['improved_avg_s']:.1f}s ({_delta_text(s['retrieval_time']['reduction'])})")
    print(f"   方法: 系统埋点 time.time 计时（整题端到端，非检索子阶段）")
    print(f"\n3. 低质信源过滤率: {s['low_quality_rate']['baseline']:.1%} → {s['low_quality_rate']['improved']:.1%}")
    print(f"   方法: _score_evidence 函数自动打分, < 0.6 计为低质")
    print(f"\n4. 幻觉率: {s['hallucination_rate']['baseline']:.1%} → {s['hallucination_rate']['improved']:.1%}")
    print(f"   方法: LLM-as-Judge 事实核查 (3轮取中位数)")
    print(f"\n5. 引用准确率: {s['citation_accuracy']['improved']:.1%}")
    print(f"   方法: 规则校验合法性 + LLM-as-Judge 语义匹配")
    print(f"\n6. Token 消耗: {s['token_reduction']['baseline_avg']} → {s['token_reduction']['improved_avg']} ({_delta_text(s['token_reduction']['reduction'])})")
    print(f"   方法: LangChain 回调 on_llm_end 累计（llm_output 为空时回落 message.usage_metadata）")
    print(f"\n7. 简单问答响应: 平均 {s['direct_response']['avg_s']:.2f}s, 最大 {s['direct_response']['max_s']:.2f}s, < 2s: {s['direct_response']['under_2s']}")
    print(f"   方法: 系统埋点 time.time 计时")
    print(f"\n8. 证据重复率: {s['evidence_duplication']['baseline']:.1%} → {s['evidence_duplication']['improved']:.1%}")
    print(f"   方法: 规则型（同一 source_id 重复出现的比例，reducer 语义哨兵）")
    print(f"\n9. 检索深度: {s['retrieval_depth']['baseline_queries_per_round']:.1f} → {s['retrieval_depth']['improved_queries_per_round']:.1f} 查询/轮")
    print(f"   方法: 规则型（检索轨迹条数 / 外层研究轮数）")
    print(f"\n10. 引用合法率: {s['citation_legality']['improved']:.1%}   要点字面覆盖: {s['key_point_coverage']['improved']:.1%}")
    print(f"   方法: 规则型（角标存在性校验 / 关键词子串匹配，对照 LLM-as-Judge）")
    src = s["expected_source_recall"]
    if src["annotated_queries"]:
        print(f"\n11. 期望来源召回: {src['baseline']:.1%} → {src['improved']:.1%}"
              f"（已标注 {src['annotated_queries']} 题）")
    else:
        print(f"\n11. 期望来源召回: 未评测 —— 题集里还没有题标注 expected_sources")
        print(f"   要判「检索有没有找对来源」必须先在 app/test/golden_set.json 里标注")
    print(f"   方法: 规则型（正文引用来源与期望域名/URL 片段比对）")
    print("\n" + "=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DeepResearch 自动化评测")
    parser.add_argument("--output", type=str, default="eval_report.json", help="输出报告路径")
    parser.add_argument("--max-queries", type=int, default=0, help="最大评测题数 (0=全部)")
    parser.add_argument("--judge-model", type=str, default="",
                        help="裁判模型（留空取 config.json 的 model）")
    parser.add_argument("--judge-rounds", type=int, default=3,
                        help="每项指标取中位数的裁判轮数；跑大样本时降到 1 可省 2/3 裁判成本")
    args = parser.parse_args()

    asyncio.run(run_eval(args.output, args.max_queries, args.judge_model, args.judge_rounds))
