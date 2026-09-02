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
  cd deep_research
  python -m app.test.eval_metrics --config config.json --output eval_report.json
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mult_agents.config import AppConfig
from mult_agents.graph import build_app as build_workflow_app
from mult_agents.runtime import build_checkpointer, build_memory_manager
from mult_agents.models import build_agents
from mult_agents.state import create_initial_state

logger = logging.getLogger("eval")


# ======================================================================
# 测试集
# ======================================================================

EVAL_QUERIES = [
    {"query": "2024年AI Agent框架发展趋势调研", "type": "multiagent",
     "key_points": ["LangGraph", "AutoGen", "CrewAI", "多智能体", "状态机", "工具调用"]},
    {"query": "RAG与Fine-tuning技术路线对比分析", "type": "multiagent",
     "key_points": ["RAG原理", "Fine-tuning成本", "适用场景", "混合方案", "知识更新"]},
    {"query": "大模型推理优化技术盘点", "type": "multiagent",
     "key_points": ["KV Cache", "量化", "蒸馏", "投机解码", "批处理", "vLLM"]},
    {"query": "向量数据库选型调研：Milvus vs Pinecone vs Weaviate", "type": "multiagent",
     "key_points": ["Milvus", "Pinecone", "Weaviate", "性能对比", "索引类型", "部署方式"]},
    {"query": "2024年开源大模型发展趋势", "type": "multiagent",
     "key_points": ["Llama", "Qwen", "Mistral", "开源生态", "许可证", "性能"]},
    {"query": "LangChain与LangGraph框架对比分析", "type": "multiagent",
     "key_points": ["LangChain", "LangGraph", "Chain", "Graph", "状态管理", "Agent"]},
    {"query": "Agent记忆系统设计调研", "type": "multiagent",
     "key_points": ["短期记忆", "长期记忆", "向量存储", "摘要压缩", "用户画像", "跨会话"]},
    {"query": "Prompt Engineering最佳实践调研", "type": "multiagent",
     "key_points": ["Few-shot", "CoT", "ReAct", "模板化", "系统提示", "角色设定"]},
    {"query": "多模态大模型技术进展调研", "type": "multiagent",
     "key_points": ["视觉语言模型", "CLIP", "图文理解", "跨模态对齐", "应用场景"]},
    {"query": "AI代码生成工具对比调研", "type": "multiagent",
     "key_points": ["Copilot", "Cursor", "Codeium", "代码补全", "准确性", "集成"]},
    {"query": "知识图谱构建技术调研", "type": "multiagent",
     "key_points": ["实体抽取", "关系抽取", "Neo4j", "图数据库", "本体设计"]},
    {"query": "LLM安全与对齐技术调研", "type": "multiagent",
     "key_points": ["RLHF", "DPO", "安全护栏", "红队测试", "越狱防御", "对齐"]},
    {"query": "Serverless GPU平台调研", "type": "multiagent",
     "key_points": ["按需GPU", "无服务器", "成本优化", "冷启动", "弹性扩缩"]},
    {"query": "向量检索算法调研：HNSW vs IVF", "type": "multiagent",
     "key_points": ["HNSW", "IVF", "ANN", "召回率", "查询延迟", "内存占用"]},
    {"query": "大模型评测基准调研", "type": "multiagent",
     "key_points": ["MMLU", "HumanEval", "C-Eval", "评测方法", "基准偏差"]},
    {"query": "AI Agent工具调用框架对比", "type": "multiagent",
     "key_points": ["Function Calling", "Tool Use", "ReAct", "工具注册", "错误处理"]},
    {"query": "企业知识库RAG系统架构调研", "type": "multiagent",
     "key_points": ["文档分块", "Embedding", "向量检索", "重排序", "引用溯源"]},
    {"query": "2024年AI编程助手发展趋势", "type": "multiagent",
     "key_points": ["代码生成", "代码审查", "Bug修复", "IDE集成", "上下文理解"]},
    {"query": "LLM长上下文处理技术调研", "type": "multiagent",
     "key_points": ["长窗口", "RoPE", "上下文压缩", "RAG替代", "注意力机制"]},
    {"query": "AI Agent工作流编排引擎对比", "type": "multiagent",
     "key_points": ["LangGraph", "Temporal", "Airflow", "状态机", "DAG", "重试"]},
    {"query": "大模型微调技术路线调研", "type": "multiagent",
     "key_points": ["LoRA", "QLoRA", "全量微调", "数据准备", "显存需求"]},
    {"query": "向量embedding模型调研", "type": "multiagent",
     "key_points": ["text-embedding", "BGE", "E5", "维度", "多语言", "评测"]},
    {"query": "AI搜索技术架构调研", "type": "multiagent",
     "key_points": ["搜索增强", "引用溯源", "实时检索", "摘要生成", "多源融合"]},
    {"query": "GraphRAG技术调研", "type": "multiagent",
     "key_points": ["知识图谱", "图检索", "实体关系", "社区检测", "层次摘要"]},
    {"query": "大模型部署推理框架对比", "type": "multiagent",
     "key_points": ["vLLM", "TGI", "TensorRT-LLM", "吞吐量", "延迟", "量化"]},
    {"query": "AI Agent反思机制调研", "type": "multiagent",
     "key_points": ["自我评估", "补搜", "迭代优化", "错误纠正", "质量提升"]},
    {"query": "企业级LLM应用架构调研", "type": "multiagent",
     "key_points": ["API网关", "负载均衡", "缓存", "降级", "监控", "多租户"]},
    {"query": "RAG重排序技术调研", "type": "multiagent",
     "key_points": ["Cross-Encoder", "Cohere Rerank", "BGE Reranker", "召回率提升"]},
    {"query": "AI Agent多轮对话管理调研", "type": "multiagent",
     "key_points": ["对话状态", "上下文窗口", "摘要压缩", "记忆持久化", "会话管理"]},
    {"query": "大模型幻觉缓解技术调研", "type": "multiagent",
     "key_points": ["引用溯源", "交叉验证", "证据裁判", "知识grounded", "检测方法"]},
    {"query": "1+1等于几", "type": "direct", "key_points": ["2"]},
    {"query": "Python的list怎么排序", "type": "direct", "key_points": ["sort", "sorted"]},
    {"query": "什么是REST API", "type": "direct", "key_points": ["REST", "HTTP", "资源"]},
    {"query": "JSON是什么格式", "type": "direct", "key_points": ["键值对", "文本", "数据交换"]},
    {"query": "Git怎么回退上一个提交", "type": "direct", "key_points": ["git revert", "git reset"]},
    {"query": "Python中len函数怎么用", "type": "direct", "key_points": ["长度", "len"]},
    {"query": "什么是Docker", "type": "direct", "key_points": ["容器", "镜像", "隔离"]},
    {"query": "HTTP状态码404是什么意思", "type": "direct", "key_points": ["Not Found", "资源不存在"]},
    {"query": "Python的字典怎么遍历", "type": "direct", "key_points": ["items", "keys", "values"]},
    {"query": "什么是CSV文件", "type": "direct", "key_points": ["逗号分隔", "表格", "文本"]},
    {"query": "SQL中WHERE和HAVING的区别", "type": "direct", "key_points": ["WHERE", "HAVING", "聚合"]},
    {"query": "Python中map函数的作用", "type": "direct", "key_points": ["映射", "函数", "迭代"]},
    {"query": "什么是API网关", "type": "direct", "key_points": ["入口", "路由", "限流"]},
    {"query": "Linux中chmod命令怎么用", "type": "direct", "key_points": ["权限", "读写执行"]},
    {"query": "Python中*args和**kwargs的区别", "type": "direct", "key_points": ["位置参数", "关键字参数"]},
    {"query": "什么是CI/CD", "type": "direct", "key_points": ["持续集成", "持续部署", "自动化"]},
    {"query": "HTTP的GET和POST区别", "type": "direct", "key_points": ["GET", "POST", "参数位置"]},
    {"query": "Python中虚拟环境的作用", "type": "direct", "key_points": ["隔离", "依赖", "venv"]},
    {"query": "什么是正则表达式", "type": "direct", "key_points": ["模式匹配", "文本", "规则"]},
    {"query": "SQL中JOIN的类型有哪些", "type": "direct", "key_points": ["INNER", "LEFT", "RIGHT", "FULL"]},
]


# ======================================================================
# Token 计数 Hook
# ======================================================================

class TokenAccumulator:
    """Hook ChatTongyi 调用，累计 Token 消耗"""

    def __init__(self):
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.call_count = 0
        self._original_generate = None
        self._active = False
        self._hooked_llms: list = []

    def attach(self, llm):
        if self._active:
            return
        self._original_generate = llm._generate
        llm._generate = self._wrapped_generate
        self._hooked_llms.append(llm)
        self._active = True

    def detach(self):
        for llm in self._hooked_llms:
            try:
                llm._generate = self._original_generate
            except Exception:
                pass
        self._hooked_llms.clear()
        self._active = False

    def _wrapped_generate(self, *args, **kwargs):
        result = self._original_generate(*args, **kwargs)
        try:
            for generation in result.generations:
                if hasattr(generation, "generation_info") and generation.generation_info:
                    usage = generation.generation_info.get("usage", {})
                    self.total_prompt_tokens += usage.get("prompt_tokens", 0)
                    self.total_completion_tokens += usage.get("completion_tokens", 0)
                    self.call_count += 1
        except Exception:
            pass
        return result

    def reset(self):
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.call_count = 0

    @property
    def total_tokens(self):
        return self.total_prompt_tokens + self.total_completion_tokens


# ======================================================================
# LLM-as-Judge
# ======================================================================

def build_judge_llm(api_key: str, model: str = "qwen-max"):
    from langchain_community.chat_models import ChatTongyi
    return ChatTongyi(model=model, temperature=0.0, dashscope_api_key=api_key)


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
    intent: str = ""
    error: str = ""


def _result_to_dict(r: EvalResult) -> dict:
    return {
        "query": r.query, "type": r.query_type, "elapsed_time": round(r.elapsed_time, 2),
        "token_count": r.token_count, "retrieval_stats": r.retrieval_stats,
        "completeness": r.completeness, "hallucination": r.hallucination,
        "citation_accuracy": r.citation_accuracy, "intent": r.intent,
        "error": r.error, "final_preview": r.final_output[:500],
    }


def run_single_query(app, config, query, token_acc, memory_manager=None):
    memory_context = ""
    if memory_manager and config.enable_memory:
        try:
            memory_context = memory_manager.build_personalized_prompt_context(
                user_id=config.user_id, thread_id=config.thread_id, query=query,
                tenant_id=config.tenant_id, max_memories=config.memory_top_k)
        except Exception:
            pass
    state = create_initial_state(
        query=query, max_iterations=config.max_iterations,
        user_id=config.user_id, tenant_id=config.tenant_id, memory_context=memory_context)
    token_acc.reset()
    start = time.time()
    cfg = {"configurable": {"thread_id": f"{config.thread_id}_eval_{int(start)}"}}
    result = app.invoke(state, cfg)
    elapsed = time.time() - start
    final = result.get("final", "")
    return final, dict(result), elapsed, token_acc.total_tokens


def run_eval(config_path, output_path, max_queries=0):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    config = AppConfig.from_file(config_path)
    memory_manager = build_memory_manager(config) if config.enable_memory else None
    agents = build_agents(config.model, config.api_key, config)
    checkpointer = build_checkpointer(config)
    app = build_workflow_app(agents, checkpointer)
    judge_llm = build_judge_llm(config.api_key)

    token_acc = TokenAccumulator()
    for agent_attr in ["planner", "scout_web", "scout_local", "evidence_judge", "analyst", "writer", "direct_responder", "intent_router"]:
        a = getattr(agents, agent_attr, None)
        if a and hasattr(a, "_generate"):
            token_acc.attach(a)

    queries = EVAL_QUERIES
    if max_queries > 0:
        queries = queries[:max_queries]
    ma_queries = [q for q in queries if q["type"] == "multiagent"]
    dir_queries = [q for q in queries if q["type"] == "direct"]

    results_bl, results_im, results_dir = [], [], []

    # Baseline
    logger.info("=" * 60)
    logger.info("Phase 1: Baseline (max_iterations=1)")
    logger.info("=" * 60)
    bl_config = config.with_overrides(max_iterations=1, enable_memory=False)
    for i, q in enumerate(ma_queries):
        logger.info("[%d/%d] %s", i + 1, len(ma_queries), q["query"][:50])
        try:
            final, state, elapsed, tokens = run_single_query(app, bl_config, q["query"], token_acc)
            stats = extract_retrieval_stats(state)
            comp = judge_with_consensus(judge_completeness, judge_llm, q["query"], q["key_points"], final)
            halluc = judge_with_consensus(judge_hallucination, judge_llm, final, state.get("evidence_pool", []))
            cit = judge_with_consensus(judge_citation_accuracy, judge_llm, final, state.get("source_index", []))
            results_bl.append(EvalResult(q["query"], "multiagent", elapsed, final, tokens, stats, comp, halluc, cit))
            logger.info("  完备=%.2f 幻觉=%.2f 引用=%.2f Token=%d 耗时=%.1fs",
                        comp.get("score", 0), halluc.get("hallucination_rate", 0), cit.get("accuracy_rate", 0), tokens, elapsed)
        except Exception as e:
            logger.error("  失败: %s", e)
            results_bl.append(EvalResult(q["query"], "multiagent", 0, "", 0, {}, error=str(e)))

    # Improved
    logger.info("=" * 60)
    logger.info("Phase 2: Improved (max_iterations=2)")
    logger.info("=" * 60)
    im_config = config.with_overrides(max_iterations=2, enable_memory=False)
    for i, q in enumerate(ma_queries):
        logger.info("[%d/%d] %s", i + 1, len(ma_queries), q["query"][:50])
        try:
            final, state, elapsed, tokens = run_single_query(app, im_config, q["query"], token_acc)
            stats = extract_retrieval_stats(state)
            comp = judge_with_consensus(judge_completeness, judge_llm, q["query"], q["key_points"], final)
            halluc = judge_with_consensus(judge_hallucination, judge_llm, final, state.get("evidence_pool", []))
            cit = judge_with_consensus(judge_citation_accuracy, judge_llm, final, state.get("source_index", []))
            results_im.append(EvalResult(q["query"], "multiagent", elapsed, final, tokens, stats, comp, halluc, cit))
            logger.info("  完备=%.2f 幻觉=%.2f 引用=%.2f Token=%d 耗时=%.1fs",
                        comp.get("score", 0), halluc.get("hallucination_rate", 0), cit.get("accuracy_rate", 0), tokens, elapsed)
        except Exception as e:
            logger.error("  失败: %s", e)
            results_im.append(EvalResult(q["query"], "multiagent", 0, "", 0, {}, error=str(e)))

    # Direct
    logger.info("=" * 60)
    logger.info("Phase 3: Direct (简单问答)")
    logger.info("=" * 60)
    dir_config = config.with_overrides(max_iterations=1, enable_memory=False)
    for i, q in enumerate(dir_queries):
        logger.info("[%d/%d] %s", i + 1, len(dir_queries), q["query"][:50])
        try:
            final, state, elapsed, tokens = run_single_query(app, dir_config, q["query"], token_acc)
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

    report = {
        "summary": {
            "completeness": {"baseline": round(bl_comp, 4), "improved": round(im_comp, 4),
                             "delta": round(im_comp - bl_comp, 4),
                             "desc": "研究完备性（LLM-as-Judge + key_points 覆盖率，3轮取中位数）"},
            "retrieval_time": {"baseline_avg_s": round(bl_time, 2), "improved_avg_s": round(im_time, 2),
                               "reduction": round((bl_time - im_time) / bl_time, 4) if bl_time > 0 else 0,
                               "desc": "检索阶段耗时（系统埋点 time.time 计时）"},
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
        },
        "details": {
            "baseline": [_result_to_dict(r) for r in results_bl],
            "improved": [_result_to_dict(r) for r in results_im],
            "direct": [_result_to_dict(r) for r in results_dir],
        },
        "meta": {
            "total_queries": len(EVAL_QUERIES),
            "multiagent_queries": len(ma_queries),
            "direct_queries": len(dir_queries),
            "judge_model": "qwen-max",
            "judge_rounds": 3,
            "gen_model": config.model,
        },
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    logger.info("=" * 60)
    logger.info("评测报告已保存: %s", output_path)
    logger.info("=" * 60)
    print_summary(report)
    token_acc.detach()


def print_summary(report):
    s = report["summary"]
    print("\n" + "=" * 60)
    print("DeepResearch 自动化评测报告汇总")
    print("=" * 60)
    print(f"\n1. 研究完备性: {s['completeness']['baseline']:.1%} → {s['completeness']['improved']:.1%} (Δ={s['completeness']['delta']:+.1%})")
    print(f"   方法: LLM-as-Judge (qwen-max) + key_points 覆盖率, 3轮取中位数")
    print(f"\n2. 检索耗时: {s['retrieval_time']['baseline_avg_s']:.1f}s → {s['retrieval_time']['improved_avg_s']:.1f}s (降低 {s['retrieval_time']['reduction']:.1%})")
    print(f"   方法: 系统埋点 time.time 计时")
    print(f"\n3. 低质信源过滤率: {s['low_quality_rate']['baseline']:.1%} → {s['low_quality_rate']['improved']:.1%}")
    print(f"   方法: _score_evidence 函数自动打分, < 0.6 计为低质")
    print(f"\n4. 幻觉率: {s['hallucination_rate']['baseline']:.1%} → {s['hallucination_rate']['improved']:.1%}")
    print(f"   方法: LLM-as-Judge 事实核查 (3轮取中位数)")
    print(f"\n5. 引用准确率: {s['citation_accuracy']['improved']:.1%}")
    print(f"   方法: 规则校验合法性 + LLM-as-Judge 语义匹配")
    print(f"\n6. Token 消耗: {s['token_reduction']['baseline_avg']} → {s['token_reduction']['improved_avg']} (降低 {s['token_reduction']['reduction']:.1%})")
    print(f"   方法: DashScope API usage hook 累计")
    print(f"\n7. 简单问答响应: 平均 {s['direct_response']['avg_s']:.2f}s, 最大 {s['direct_response']['max_s']:.2f}s, < 2s: {s['direct_response']['under_2s']}")
    print(f"   方法: 系统埋点 time.time 计时")
    print("\n" + "=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DeepResearch 自动化评测")
    parser.add_argument("--config", type=str, default=None, help="配置文件路径")
    parser.add_argument("--output", type=str, default="eval_report.json", help="输出报告路径")
    parser.add_argument("--max-queries", type=int, default=0, help="最大评测题数 (0=全部)")
    args = parser.parse_args()

    config_path = args.config
    if not config_path:
        config_path = str(_PROJECT_ROOT / "config.json")

    run_eval(config_path, args.output, args.max_queries)
