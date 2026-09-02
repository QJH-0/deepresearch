"""
Advanced RAG 入库脚本 — 基于语义感知切片 + Parent-Child 策略

使用方法:
    python ingest.py                          # 使用默认配置入库
    python ingest.py --input /path/to/docs   # 指定输入目录
    python ingest.py --drop-old               # 清空旧集合后重新入库

改进点：
1. 按 Markdown 标题结构切分，保留层级上下文路径
2. Parent-Child 分块策略，子块精确检索 + 父块上下文增强
3. 原始文本块持久化到 PostgreSQL，支持 PgSQL 全文检索混合检索
4. 入库后自动验证检索质量
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from mult_agents.src.mult_agents.rag.core import RAGSystem
from mult_agents_memory.app.mult_agents.config import AppConfig
from mult_agents_memory.app.mult_agents.rag.core import RAGConfig

# 将项目根目录添加到 PYTHONPATH
project_root = Path(__file__).resolve().parents[3]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# 先加载 .env
from dotenv import load_dotenv
env_path = project_root / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)


# ==============================================================================
# 默认配置
# ==============================================================================
DEFAULT_INPUT_PATH = Path(project_root / "data" / "knowledge")
COLLECTION_NAME = ""
MILVUS_HOST = ""
MILVUS_PORT = 0
EMBEDDING_MODEL = "text-embedding-v1"

# 切片参数
CHUNK_SIZE = 512
CHUNK_OVERLAP = 64
PARENT_CHUNK_SIZE = 2048
PARENT_CHUNK_OVERLAP = 100

# 检索参数
RECALL_K = 20
FINAL_TOP_K = 5


def _collect_paths(input_path: Path) -> list[Path]:
    """收集输入路径下所有可入库的文档文件。"""
    if input_path.is_file():
        return [input_path]
    patterns = ("*.txt", "*.md", "*.markdown", "*.pdf", "*.docx", "*.html", "*.csv", "*.json")
    paths: list[Path] = []
    for pat in patterns:
        paths.extend(sorted(input_path.rglob(pat)))
    return paths


def _run_eval(rag: RAGSystem):
    """入库后对检索质量做基本验证。"""
    test_cases = [
        {
            "query": "AI Agent 的核心组件有哪些？",
            "expect_keywords": ["Agent", "工具", "记忆", "规划"],
        },
        {
            "query": "RAG 技术的检索增强原理是什么？",
            "expect_keywords": ["RAG", "检索", "生成", "向量"],
        },
        {
            "query": "LangGraph 的状态管理机制",
            "expect_keywords": ["LangGraph", "状态", "图"],
        },
    ]

    logger = logging.getLogger("RAGEval")
    logger.info("\n" + "=" * 60)
    logger.info("📊 检索质量验证开始")
    logger.info("=" * 60)

    pass_count = 0
    for i, tc in enumerate(test_cases):
        logger.info("\n--- 测试用例 %d/%d ---", i + 1, len(test_cases))
        logger.info("查询: %s", tc["query"])

        records = rag.search_records(tc["query"], k=3)
        combined_text = " ".join(r.get("snippet", "") for r in records)

        hit_keywords = []
        miss_keywords = []
        for kw in tc["expect_keywords"]:
            if kw.lower() in combined_text.lower():
                hit_keywords.append(kw)
            else:
                miss_keywords.append(kw)

        hit_rate = len(hit_keywords) / len(tc["expect_keywords"]) if tc["expect_keywords"] else 0
        status = "✅ PASS" if hit_rate >= 0.5 else "❌ FAIL"

        if hit_rate >= 0.5:
            pass_count += 1

        logger.info("命中关键词: %s/%s", len(hit_keywords), len(tc["expect_keywords"]))
        logger.info("已命中: %s", hit_keywords)
        if miss_keywords:
            logger.info("未命中: %s", miss_keywords)
        logger.info("命中率: %.0f%% | %s", hit_rate * 100, status)

    logger.info("\n" + "=" * 60)
    logger.info("📊 检索质量验证完成: %d/%d 通过 (%.0f%%)",
                pass_count, len(test_cases),
                pass_count / len(test_cases) * 100 if test_cases else 0)
    logger.info("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Advanced RAG 入库脚本")
    parser.add_argument("--input", type=str, default=None,
                        help="输入文件或目录路径")
    parser.add_argument("--collection", type=str, default=None,
                        help="Milvus collection 名称")
    parser.add_argument("--host", type=str, default=None,
                        help="Milvus host")
    parser.add_argument("--port", type=int, default=None,
                        help="Milvus port")
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE,
                        help="子块大小")
    parser.add_argument("--chunk-overlap", type=int, default=CHUNK_OVERLAP,
                        help="子块重叠")
    parser.add_argument("--parent-chunk-size", type=int, default=PARENT_CHUNK_SIZE,
                        help="父块大小")
    parser.add_argument("--parent-chunk-overlap", type=int, default=PARENT_CHUNK_OVERLAP,
                        help="父块重叠")
    parser.add_argument("--recall-k", type=int, default=RECALL_K,
                        help="多路召回数量")
    parser.add_argument("--top-k", type=int, default=FINAL_TOP_K,
                        help="最终返回 Top-K")
    parser.add_argument("--no-query-rewrite", action="store_true",
                        help="禁用查询重写")
    parser.add_argument("--no-fulltext", action="store_true",
                        help="禁用 PgSQL 全文检索")
    parser.add_argument("--no-reranker", action="store_true",
                        help="禁用 LLM 重排序")
    parser.add_argument("--no-parent-child", action="store_true",
                        help="禁用 Parent-Child 上下文扩展")
    parser.add_argument("--postgres-dsn", type=str, default=None,
                        help="PostgreSQL 连接串 (用于全文检索存储原始文本块)")
    parser.add_argument("--eval", action="store_true",
                        help="入库后运行评测")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )
    logger = logging.getLogger("RAGIngest")

    # 从配置文件加载配置
    config = AppConfig.from_file()
    collection_name = args.collection or COLLECTION_NAME or config.milvus_collection
    milvus_host = args.host or MILVUS_HOST or config.milvus_host
    milvus_port = args.port or MILVUS_PORT or config.milvus_port

    rag_cfg = RAGConfig(
        milvus_host=milvus_host,
        milvus_port=milvus_port,
        collection_name=collection_name,
        embedding_model=EMBEDDING_MODEL,
        chunk_size=args.chunk_size,
        chunk_overlap=args.chunk_overlap,
        parent_chunk_size=args.parent_chunk_size,
        parent_chunk_overlap=args.parent_chunk_overlap,
        recall_k=args.recall_k,
        final_top_k=args.top_k,
        enable_query_rewrite=not args.no_query_rewrite,
        enable_fulltext=not args.no_fulltext,
        enable_reranker=not args.no_reranker,
        enable_parent_child=not args.no_parent_child,
        postgres_dsn=args.postgres_dsn or getattr(config, 'postgres_dsn', ''),
    )
    rag = RAGSystem(api_key=config.api_key, config=rag_cfg)

    # 确定输入路径
    input_path_str = args.input or str(DEFAULT_INPUT_PATH)
    input_path = Path(input_path_str).expanduser().resolve()

    if not input_path.exists():
        logger.error("❌ 输入路径不存在: %s", input_path)
        sys.exit(1)

    paths = _collect_paths(input_path)
    if not paths:
        logger.error("❌ 未找到可入库文件: %s", input_path)
        sys.exit(1)

    logger.info("📂 入库开始 | 文件数=%d | 输入=%s", len(paths), input_path)
    logger.info("🔧 切片参数: 子块=%d/%d, 父块=%d/%d",
                args.chunk_size, args.chunk_overlap,
                args.parent_chunk_size, args.parent_chunk_overlap)
    logger.info("🔧 检索参数: recall_k=%d, top_k=%d", args.recall_k, args.top_k)
    logger.info("🔧 功能开关: 查询重写=%s, 全文检索=%s, 重排序=%s, Parent-Child=%s",
                not args.no_query_rewrite, not args.no_fulltext,
                not args.no_reranker, not args.no_parent_child)

    total_chunks = rag.ingest_paths(paths)
    logger.info("🎉 入库完成 | 文件数=%d | chunk数=%d | collection=%s",
                len(paths), total_chunks, collection_name)

    if args.eval:
        _run_eval(rag)


if __name__ == "__main__":
    main()
