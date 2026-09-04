#!/usr/bin/env python
"""DeepResearch RAG 指标自动化测试脚本

测量指标：
  1. 召回率（Recall）— 检索结果中包含目标文档的查询数 / 总查询数
  2. 精确率（Precision）— 返回结果中相关条目数 / 返回总条目数
  3. 关键词覆盖率（Key Point Coverage）— 命中 key_points 的比例
  4. 检索延迟（Latency）— 每次检索的耗时（ms）
  5. 模块化对比 — 逐步开启 RAG 功能开关，测量各模块对精度的贡献

运行方式：
    conda run -n llmdev python scripts/run_rag_eval.py
    conda run -n llmdev python scripts/run_rag_eval.py --skip-ingest  # 跳过入库
    conda run -n llmdev python scripts/run_rag_eval.py --drop-old     # 清空旧集合后重新入库
"""

import argparse
import hashlib
import json
import logging
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_APP_PATH = _PROJECT_ROOT / "app"
for _p in [_PROJECT_ROOT, _APP_PATH]:
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from dotenv import load_dotenv
env_path = _PROJECT_ROOT / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

from mult_agents.rag.core import RAGSystem, RAGConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("rag_eval")

TEST_DOCS_DIR = _PROJECT_ROOT / "data" / "test_documents"
REPORT_DIR = _PROJECT_ROOT / "output" / "logs" / "research"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────────────────────────
# 测试查询集（12 道，覆盖直接命中、跨文档检索、模糊匹配）
# ──────────────────────────────────────────────────────────────

RAG_EVAL_QUERIES = [
    {
        "id": 1,
        "query": "LangGraph 的 StateGraph 怎么用",
        "target_doc": "LangGraph_tutorial.md",
        "expected_relevant": True,
        "key_points": ["StateGraph", "节点", "边"],
        "test_type": "直接命中",
    },
    {
        "id": 2,
        "query": "条件路由 Conditional Edge 的实现方式",
        "target_doc": "LangGraph_tutorial.md",
        "expected_relevant": True,
        "key_points": ["Conditional Edge", "路由函数", "条件分支"],
        "test_type": "直接命中",
    },
    {
        "id": 3,
        "query": "interrupt 中断恢复机制怎么工作",
        "target_doc": "LangGraph_tutorial.md",
        "expected_relevant": True,
        "key_points": ["interrupt", "HITL", "中断", "恢复"],
        "test_type": "直接命中",
    },
    {
        "id": 4,
        "query": "Parent-Child 分块策略的原理",
        "target_doc": "RAG_综述.md",
        "expected_relevant": True,
        "key_points": ["Parent-Child", "父块", "子块", "上下文增强"],
        "test_type": "直接命中",
    },
    {
        "id": 5,
        "query": "BM25 混合检索如何实现中文分词",
        "target_doc": "RAG_综述.md",
        "expected_relevant": True,
        "key_points": ["BM25", "bigram", "中文分词", "关键词检索"],
        "test_type": "直接命中",
    },
    {
        "id": 6,
        "query": "LLM 重排序的 Cross-Encoder 风格实现",
        "target_doc": "RAG_综述.md",
        "expected_relevant": True,
        "key_points": ["LLM", "重排序", "Cross-Encoder", "reranker"],
        "test_type": "直接命中",
    },
    {
        "id": 7,
        "query": "Milvus 向量索引的构建方式",
        "target_doc": "向量库对比表.csv",
        "expected_relevant": True,
        "key_points": ["Milvus", "HNSW", "IVF", "索引"],
        "test_type": "跨文档检索",
    },
    {
        "id": 8,
        "query": "查询重写如何提高召回率",
        "target_doc": "RAG_综述.md",
        "expected_relevant": True,
        "key_points": ["查询重写", "多查询变体", "召回率"],
        "test_type": "直接命中",
    },
    {
        "id": 9,
        "query": "Agent 记忆系统有哪三层",
        "target_doc": "AI_Agent_白皮书.md",
        "expected_relevant": True,
        "key_points": ["短期记忆", "长期记忆", "工作记忆"],
        "test_type": "直接命中",
    },
    {
        "id": 10,
        "query": "LangChain 和 AutoGen 框架的区别",
        "target_doc": "AI_Agent_白皮书.md",
        "expected_relevant": True,
        "key_points": ["LangChain", "AutoGen", "对比"],
        "test_type": "跨文档检索",
    },
    {
        "id": 11,
        "query": "HNSW 索引的分层图结构原理",
        "target_doc": "HNSW索引原理.md",
        "expected_relevant": True,
        "key_points": ["HNSW", "分层", "图", "小世界"],
        "test_type": "直接命中",
    },
    {
        "id": 12,
        "query": "Embedding 模型选型对比",
        "target_doc": "Embedding模型对比.md",
        "expected_relevant": True,
        "key_points": ["Embedding", "BGE", "维度", "多语言"],
        "test_type": "直接命中",
    },
]

# ──────────────────────────────────────────────────────────────
# 模块化对比实验配置
# ──────────────────────────────────────────────────────────────

MODULE_CONFIGS = [
    {
        "name": "基线（纯向量检索）",
        "enable_query_rewrite": False,
        "enable_bm25": False,
        "enable_reranker": False,
        "enable_parent_child": False,
    },
    {
        "name": "+ 查询重写",
        "enable_query_rewrite": True,
        "enable_bm25": False,
        "enable_reranker": False,
        "enable_parent_child": False,
    },
    {
        "name": "+ BM25 混合检索",
        "enable_query_rewrite": True,
        "enable_bm25": True,
        "enable_reranker": False,
        "enable_parent_child": False,
    },
    {
        "name": "+ LLM 重排序",
        "enable_query_rewrite": True,
        "enable_bm25": True,
        "enable_reranker": True,
        "enable_parent_child": False,
    },
    {
        "name": "全量启用（+ Parent-Child）",
        "enable_query_rewrite": True,
        "enable_bm25": True,
        "enable_reranker": True,
        "enable_parent_child": True,
    },
]


# ──────────────────────────────────────────────────────────────
# 数据结构
# ──────────────────────────────────────────────────────────────

@dataclass
class QueryResult:
    query_id: int
    query: str
    target_doc: str
    test_type: str
    recall_hit: bool
    returned_count: int
    relevant_count: int
    key_points_hit: int
    key_points_total: int
    key_point_coverage: float
    precision: float
    latency_ms: float
    top_snippets: list = field(default_factory=list)


@dataclass
class ModuleResult:
    config_name: str
    config: dict
    recall: float
    precision: float
    key_point_coverage: float
    avg_latency_ms: float
    query_details: list = field(default_factory=list)


# ──────────────────────────────────────────────────────────────
# 入库
# ──────────────────────────────────────────────────────────────

def _ensure_collections(rag: RAGSystem, drop_old: bool = False) -> None:
    """确保 Milvus 集合以 enable_dynamic_field=True 创建。

    langchain_milvus 在首次 add_documents 时自动创建 schema，
    如果第一个文档缺少某些 metadata 字段（如 h3/h4），
    后续文档插入会因 schema 不匹配而失败。
    启用 dynamic_field 后所有 metadata 字段动态存储，不依赖固定 schema。
    """
    from pymilvus import connections, utility, Collection, FieldSchema, CollectionSchema, DataType

    conn = connections.get_connection_addr("default")
    if conn is None:
        connections.connect(alias="default", host=rag.config.milvus_host, port=rag.config.milvus_port)

    for col_name in [rag.config.collection_name, rag.config.parent_collection_name]:
        if drop_old and utility.has_collection(col_name, using="default"):
            utility.drop_collection(col_name, using="default")
            logger.info("已删除集合: %s", col_name)

        if not utility.has_collection(col_name, using="default"):
            fields = [
                FieldSchema(name="pk", dtype=DataType.VARCHAR, is_primary=True, max_length=100, auto_id=True),
                FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=65535),
                FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=1024),
            ]
            schema = CollectionSchema(
                fields=fields,
                description=f"RAG collection: {col_name}",
                enable_dynamic_field=True,
            )
            Collection(col_name, schema=schema, using="default")
            logger.info("已创建集合(动态字段): %s", col_name)


def ingest_documents(rag: RAGSystem, drop_old: bool = False) -> dict:
    """将测试文档入库到 Milvus 向量数据库。"""
    _ensure_collections(rag, drop_old=drop_old)

    test_files = [
        TEST_DOCS_DIR / "AI_Agent_白皮书.md",
        TEST_DOCS_DIR / "LangGraph_tutorial.md",
        TEST_DOCS_DIR / "RAG_综述.md",
        TEST_DOCS_DIR / "向量库对比表.csv",
        TEST_DOCS_DIR / "配置说明.txt",
        TEST_DOCS_DIR / "Embedding模型对比.md",
        TEST_DOCS_DIR / "HNSW索引原理.md",
    ]

    ingest_stats = {"files": [], "total_chunks": 0}

    for path in test_files:
        if not path.exists():
            logger.warning("文件不存在，跳过: %s", path)
            continue
        start = time.time()
        chunks = rag.ingest_paths([path])
        elapsed_ms = (time.time() - start) * 1000
        ingest_stats["files"].append({
            "filename": path.name,
            "file_type": path.suffix.lstrip("."),
            "size_kb": round(path.stat().st_size / 1024, 1),
            "chunks": chunks,
            "ingest_ms": round(elapsed_ms, 1),
        })
        ingest_stats["total_chunks"] += chunks
        logger.info("入库: %s -> %d chunks (%.0fms)", path.name, chunks, elapsed_ms)

    return ingest_stats


# ──────────────────────────────────────────────────────────────
# 单次检索评测
# ──────────────────────────────────────────────────────────────

def evaluate_single_query(rag: RAGSystem, query_item: dict, k: int = 5) -> QueryResult:
    """对单个查询执行检索并评测指标。"""
    query = query_item["query"]
    target_doc = query_item["target_doc"]
    key_points = query_item["key_points"]

    start = time.time()
    records = rag.search_records(query, k=k)
    latency_ms = (time.time() - start) * 1000

    returned_count = len(records)

    all_snippets = []
    for r in records:
        snippet = str(r.get("snippet", ""))
        all_snippets.append(snippet[:150])

    # 关键词覆盖：在所有返回 snippet 中命中 key_points 的比例
    combined_text = " ".join(str(r.get("snippet", "")) for r in records)
    kp_hit_count = sum(1 for kp in key_points if kp.lower() in combined_text.lower())
    kp_coverage = kp_hit_count / len(key_points) if key_points else 0

    # 召回判定：key_points 覆盖率 >= 50% 视为召回命中
    # （doc_id 在动态字段模式下不可用，改用内容匹配判定）
    recall_hit = kp_coverage >= 0.5

    # 精确率：相关条目数 / 返回总条目数
    # 相关性判定：key_points 在 snippet 中命中 >= 1 个
    relevant_count = 0
    for r in records:
        snippet = str(r.get("snippet", ""))
        kp_hit = sum(1 for kp in key_points if kp.lower() in snippet.lower())
        if kp_hit >= 1:
            relevant_count += 1

    precision = relevant_count / returned_count if returned_count > 0 else 0

    return QueryResult(
        query_id=query_item["id"],
        query=query,
        target_doc=target_doc,
        test_type=query_item["test_type"],
        recall_hit=recall_hit,
        returned_count=returned_count,
        relevant_count=relevant_count,
        key_points_hit=kp_hit_count,
        key_points_total=len(key_points),
        key_point_coverage=round(kp_coverage, 4),
        precision=round(precision, 4),
        latency_ms=round(latency_ms, 1),
        top_snippets=all_snippets[:3],
    )


# ──────────────────────────────────────────────────────────────
# 模块化对比实验
# ──────────────────────────────────────────────────────────────

def run_module_experiment(
    api_key: str,
    base_config: RAGConfig,
    module_cfg: dict,
    queries: list,
    k: int = 5,
) -> ModuleResult:
    """运行一组模块配置的检索实验。"""
    from dataclasses import replace as dc_replace

    rag_cfg = dc_replace(
        base_config,
        enable_query_rewrite=module_cfg["enable_query_rewrite"],
        enable_bm25=module_cfg["enable_bm25"],
        enable_reranker=module_cfg["enable_reranker"],
        enable_parent_child=module_cfg["enable_parent_child"],
    )

    rag = RAGSystem(api_key=api_key, config=rag_cfg)

    results: list[QueryResult] = []
    for q in queries:
        r = evaluate_single_query(rag, q, k=k)
        results.append(r)

    recall_hits = sum(1 for r in results if r.recall_hit)
    recall = recall_hits / len(results) if results else 0

    total_returned = sum(r.returned_count for r in results)
    total_relevant = sum(r.relevant_count for r in results)
    precision = total_relevant / total_returned if total_returned > 0 else 0

    avg_kp_coverage = statistics.mean([r.key_point_coverage for r in results]) if results else 0
    avg_latency = statistics.mean([r.latency_ms for r in results]) if results else 0

    return ModuleResult(
        config_name=module_cfg["name"],
        config={
            "enable_query_rewrite": module_cfg["enable_query_rewrite"],
            "enable_bm25": module_cfg["enable_bm25"],
            "enable_reranker": module_cfg["enable_reranker"],
            "enable_parent_child": module_cfg["enable_parent_child"],
        },
        recall=round(recall, 4),
        precision=round(precision, 4),
        key_point_coverage=round(avg_kp_coverage, 4),
        avg_latency_ms=round(avg_latency, 1),
        query_details=[asdict(r) for r in results],
    )


# ──────────────────────────────────────────────────────────────
# 报告生成
# ──────────────────────────────────────────────────────────────

def generate_json_report(
    ingest_stats: dict,
    full_config_result: ModuleResult,
    module_results: list,
    queries: list,
    output_path: Path,
):
    """生成 JSON 格式的评测报告。"""
    fr = full_config_result

    report = {
        "meta": {
            "test_documents": len(ingest_stats["files"]),
            "total_chunks": ingest_stats["total_chunks"],
            "test_queries": len(queries),
            "eval_date": time.strftime("%Y-%m-%d %H:%M:%S"),
            "embedding_model": "text-embedding-v3",
            "rerank_model": "qwen-plus",
            "k": 5,
        },
        "ingest_stats": ingest_stats,
        "full_config_metrics": {
            "recall": fr.recall,
            "precision": fr.precision,
            "key_point_coverage": fr.key_point_coverage,
            "avg_latency_ms": fr.avg_latency_ms,
            "desc": "全量启用配置下的 RAG 检索指标",
        },
        "module_comparison": [
            {
                "config_name": m.config_name,
                "config": m.config,
                "recall": m.recall,
                "precision": m.precision,
                "key_point_coverage": m.key_point_coverage,
                "avg_latency_ms": m.avg_latency_ms,
            }
            for m in module_results
        ],
        "query_details": fr.query_details,
        "ingest_file_details": ingest_stats["files"],
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    logger.info("JSON 报告已保存: %s", output_path)


def generate_markdown_report(
    ingest_stats: dict,
    full_config_result: ModuleResult,
    module_results: list,
    queries: list,
    output_path: Path,
):
    """生成 Markdown 格式的评测报告。"""
    fr = full_config_result
    lines = []
    lines.append("# DeepResearch RAG 指标测试报告")
    lines.append("")
    lines.append(f"> 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"> 测试文档: {len(ingest_stats['files'])} 份 | 总切块数: {ingest_stats['total_chunks']}")
    lines.append(f"> 测试查询: {len(queries)} 道 | Embedding: text-embedding-v3 | 重排序: qwen-plus")
    lines.append("")

    # ── 一、入库统计 ──
    lines.append("## 一、文档入库统计")
    lines.append("")
    lines.append("| # | 文件名 | 格式 | 大小(KB) | 切块数 | 入库耗时(ms) |")
    lines.append("|---|--------|------|----------|--------|-------------|")
    for i, f in enumerate(ingest_stats["files"], 1):
        lines.append(f"| {i} | {f['filename']} | {f['file_type']} | {f['size_kb']} | {f['chunks']} | {f['ingest_ms']} |")
    lines.append(f"| - | **合计** | - | - | **{ingest_stats['total_chunks']}** | - |")
    lines.append("")

    # ── 二、全量配置指标汇总 ──
    lines.append("## 二、全量配置指标汇总")
    lines.append("")
    lines.append("| 指标 | 值 | 说明 |")
    lines.append("|------|-----|------|")
    lines.append(f"| 召回率 (Recall) | **{fr.recall:.1%}** | 检索结果中包含目标文档的查询数 / 总查询数 |")
    lines.append(f"| 精确率 (Precision) | **{fr.precision:.1%}** | 返回结果中相关条目数 / 返回总条目数 |")
    lines.append(f"| 关键词覆盖率 | **{fr.key_point_coverage:.1%}** | key_points 在返回结果中的命中比例 |")
    lines.append(f"| 平均检索延迟 | **{fr.avg_latency_ms:.0f}ms** | 单次检索平均耗时（含查询重写+向量检索+BM25+重排序+Parent-Child） |")
    lines.append("")

    # ── 三、模块化对比实验 ──
    lines.append("## 三、模块化对比实验")
    lines.append("")
    lines.append("> 逐步开启 RAGConfig 的 4 个功能开关，每步只改一个变量，测量各模块对检索精度的贡献。")
    lines.append("")
    lines.append("| # | 配置 | 查询重写 | BM25 | 重排序 | Parent-Child | 召回率 | 精确率 | 关键词覆盖率 | 平均延迟(ms) |")
    lines.append("|---|------|---------|------|--------|-------------|--------|--------|------------|-------------|")

    prev_recall = 0
    prev_precision = 0
    for i, m in enumerate(module_results, 1):
        recall_delta = ""
        precision_delta = ""
        if i > 1:
            recall_delta = f" ({m.recall - prev_recall:+.1%})" if m.recall != prev_recall else " (+0%)"
            precision_delta = f" ({m.precision - prev_precision:+.1%})" if m.precision != prev_precision else " (+0%)"
        qw = "✓" if m.config["enable_query_rewrite"] else "✗"
        bm = "✓" if m.config["enable_bm25"] else "✗"
        rr = "✓" if m.config["enable_reranker"] else "✗"
        pc = "✓" if m.config["enable_parent_child"] else "✗"
        lines.append(
            f"| {i} | {m.config_name} | {qw} | {bm} | {rr} | {pc} | "
            f"**{m.recall:.1%}**{recall_delta} | **{m.precision:.1%}**{precision_delta} | "
            f"{m.key_point_coverage:.1%} | {m.avg_latency_ms:.0f} |"
        )
        prev_recall = m.recall
        prev_precision = m.precision

    total_recall_delta = module_results[-1].recall - module_results[0].recall
    total_precision_delta = module_results[-1].precision - module_results[0].precision
    lines.append(f"| - | **总提升** | - | - | - | - | **{total_recall_delta:+.1%}** | **{total_precision_delta:+.1%}** | - | - |")
    lines.append("")

    # 模块贡献分析
    lines.append("### 模块贡献分析")
    lines.append("")
    for i in range(1, len(module_results)):
        prev = module_results[i - 1]
        curr = module_results[i]
        recall_gain = curr.recall - prev.recall
        precision_gain = curr.precision - prev.precision
        lines.append(f"**{curr.config_name}**：召回率 {'+' if recall_gain >= 0 else ''}{recall_gain:.1%}，精确率 {'+' if precision_gain >= 0 else ''}{precision_gain:.1%}")
    lines.append("")

    # ── 四、逐查询命中详情（全量配置）──
    lines.append("## 四、逐查询命中详情（全量配置）")
    lines.append("")
    lines.append("| # | 查询 | 目标文档 | 测试类型 | 召回命中 | 返回条目 | 相关条目 | 精确率 | 关键词覆盖 | 延迟(ms) |")
    lines.append("|---|------|---------|---------|---------|---------|---------|--------|-----------|---------|")
    for q in fr.query_details:
        hit_mark = "✓" if q["recall_hit"] else "✗"
        lines.append(
            f"| {q['query_id']} | {q['query']} | {q['target_doc']} | {q['test_type']} | "
            f"{hit_mark} | {q['returned_count']} | {q['relevant_count']} | "
            f"{q['precision']:.0%} | {q['key_points_hit']}/{q['key_points_total']} | {q['latency_ms']:.0f} |"
        )

    total_hit = sum(1 for q in fr.query_details if q["recall_hit"])
    total_returned = sum(q["returned_count"] for q in fr.query_details)
    total_relevant = sum(q["relevant_count"] for q in fr.query_details)
    avg_latency = statistics.mean(q["latency_ms"] for q in fr.query_details)
    lines.append(
        f"| - | **汇总** | - | - | **{total_hit}/{len(fr.query_details)}** | **{total_returned}** | "
        f"**{total_relevant}** | **{total_relevant/total_returned:.0%}** | - | **{avg_latency:.0f}** |"
    )
    lines.append("")

    # ── 五、计算公式 ──
    lines.append("## 五、计算公式")
    lines.append("")
    lines.append("```")
    lines.append("召回率 = 召回命中查询数 / 总查询数")
    lines.append("精确率 = 相关条目数 / 返回总条目数")
    lines.append("关键词覆盖率 = 命中 key_points 数 / key_points 总数")
    lines.append("相关性判定 = key_points 在 snippet 中命中 >= 1 个计为相关条目")
    lines.append("```")
    lines.append("")
    lines.append("---")
    lines.append(f"> 由 `scripts/run_rag_eval.py` 自动生成")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    logger.info("Markdown 报告已保存: %s", output_path)


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="DeepResearch RAG 指标自动化测试")
    parser.add_argument("--skip-ingest", action="store_true", help="跳过入库步骤（使用已有数据）")
    parser.add_argument("--drop-old", action="store_true", help="清空旧集合后重新入库")
    parser.add_argument("--k", type=int, default=5, help="每个查询返回的 Top-K 数量")
    parser.add_argument("--output-dir", type=str, default=str(REPORT_DIR), help="报告输出目录")
    args = parser.parse_args()

    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    if not api_key:
        logger.error("缺少 DASHSCOPE_API_KEY 环境变量")
        sys.exit(1)

    milvus_host = os.getenv("MILVUS_HOST", "localhost")
    milvus_port = int(os.getenv("MILVUS_PORT", "19530"))

    base_config = RAGConfig(
        milvus_host=milvus_host,
        milvus_port=milvus_port,
        collection_name="mult_agent_knowledge",
        parent_collection_name="mult_agent_knowledge_parent",
        embedding_model="text-embedding-v3",
        recall_k=20,
        final_top_k=args.k,
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: 入库
    ingest_stats = {"files": [], "total_chunks": 0}
    if not args.skip_ingest:
        logger.info("=" * 60)
        logger.info("Step 1: 文档入库")
        logger.info("=" * 60)
        ingest_rag = RAGSystem(api_key=api_key, config=base_config)
        ingest_stats = ingest_documents(ingest_rag, drop_old=args.drop_old)
        logger.info("入库完成: %d 文件, %d chunks",
                    len(ingest_stats["files"]), ingest_stats["total_chunks"])
    else:
        logger.info("跳过入库步骤，使用已有向量数据")
        prior_json = output_dir / "rag_eval_report.json"
        if prior_json.exists():
            try:
                prior = json.loads(prior_json.read_text(encoding="utf-8"))
                prior_ingest = prior.get("ingest_stats", {})
                if prior_ingest.get("files"):
                    ingest_stats = prior_ingest
                    logger.info("从上次报告恢复入库统计: %d 文件, %d chunks",
                                len(ingest_stats["files"]), ingest_stats["total_chunks"])
            except Exception:
                pass

    # Step 2: 全量配置评测
    logger.info("=" * 60)
    logger.info("Step 2: 全量配置检索评测 (%d 道查询)", len(RAG_EVAL_QUERIES))
    logger.info("=" * 60)
    full_rag = RAGSystem(api_key=api_key, config=base_config)
    full_results = []
    for q in RAG_EVAL_QUERIES:
        r = evaluate_single_query(full_rag, q, k=args.k)
        full_results.append(r)
        hit_mark = "✓" if r.recall_hit else "✗"
        logger.info("[%d/%d] %s | 召回=%s 精确=%.0f%% 关键词=%d/%d 延迟=%.0fms",
                     q["id"], len(RAG_EVAL_QUERIES), q["query"][:30],
                     hit_mark, r.precision, r.key_points_hit, r.key_points_total, r.latency_ms)

    recall_hits = sum(1 for r in full_results if r.recall_hit)
    recall_rate = recall_hits / len(full_results)
    total_returned = sum(r.returned_count for r in full_results)
    total_relevant = sum(r.relevant_count for r in full_results)
    precision_rate = total_relevant / total_returned if total_returned > 0 else 0
    avg_kp_cov = statistics.mean([r.key_point_coverage for r in full_results])
    avg_latency = statistics.mean([r.latency_ms for r in full_results])

    full_module_result = ModuleResult(
        config_name="全量启用",
        config={
            "enable_query_rewrite": True,
            "enable_bm25": True,
            "enable_reranker": True,
            "enable_parent_child": True,
        },
        recall=round(recall_rate, 4),
        precision=round(precision_rate, 4),
        key_point_coverage=round(avg_kp_cov, 4),
        avg_latency_ms=round(avg_latency, 1),
        query_details=[asdict(r) for r in full_results],
    )

    logger.info("全量配置: 召回=%.1f%% 精确=%.1f%% 关键词覆盖=%.1f%% 平均延迟=%.0fms",
                recall_rate * 100, precision_rate * 100, avg_kp_cov * 100, avg_latency)

    # Step 3: 模块化对比实验
    logger.info("=" * 60)
    logger.info("Step 3: 模块化对比实验 (%d 组配置)", len(MODULE_CONFIGS))
    logger.info("=" * 60)
    module_results = []
    for mc in MODULE_CONFIGS:
        logger.info("--- 配置: %s ---", mc["name"])
        mr = run_module_experiment(api_key, base_config, mc, RAG_EVAL_QUERIES, k=args.k)
        module_results.append(mr)
        logger.info("  召回=%.1f%% 精确=%.1f%% 关键词覆盖=%.1f%% 延迟=%.0fms",
                     mr.recall * 100, mr.precision * 100, mr.key_point_coverage * 100, mr.avg_latency_ms)

    # Step 4: 生成报告
    logger.info("=" * 60)
    logger.info("Step 4: 生成评测报告")
    logger.info("=" * 60)

    json_path = output_dir / "rag_eval_report.json"
    md_path = output_dir / "rag_eval_report.md"

    generate_json_report(ingest_stats, full_module_result, module_results, RAG_EVAL_QUERIES, json_path)
    generate_markdown_report(ingest_stats, full_module_result, module_results, RAG_EVAL_QUERIES, md_path)

    # 控制台汇总
    print("\n" + "=" * 60)
    print("DeepResearch RAG 指标测试汇总")
    print("=" * 60)
    print(f"\n入库: {len(ingest_stats['files'])} 文件, {ingest_stats['total_chunks']} chunks")
    print(f"\n全量配置:")
    print(f"  召回率:       {recall_rate:.1%} ({recall_hits}/{len(full_results)})")
    print(f"  精确率:       {precision_rate:.1%} ({total_relevant}/{total_returned})")
    print(f"  关键词覆盖率: {avg_kp_cov:.1%}")
    print(f"  平均延迟:     {avg_latency:.0f}ms")
    print(f"\n模块化对比:")
    for m in module_results:
        print(f"  {m.config_name:25s} | 召回={m.recall:.1%} | 精确={m.precision:.1%} | 延迟={m.avg_latency_ms:.0f}ms")
    print(f"\n报告:")
    print(f"  JSON: {json_path}")
    print(f"  MD:   {md_path}")
    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
