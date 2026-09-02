"""
Milvus 向量数据库查看工具

功能：
1. 查看集合列表和基本信息
2. 查看子块/父块集合中的数据（文本 + metadata + 向量维度）
3. 按 source/source_name 过滤查看特定文档的数据
4. 统计各文档的 chunk 数量
5. 查看集合 schema 和索引信息

使用方式：
    # 命令行
    python -m mult_agents.rag.db_viewer --list-collections
    python -m mult_agents.rag.db_viewer --show-child --limit 10
    python -m mult_agents.rag.db_viewer --show-parent --limit 5
    python -m mult_agents.rag.db_viewer --filter-source "DeepResearch" --show-child
    python -m mult_agents.rag.db_viewer --stats
    python -m mult_agents.rag.db_viewer --schema

    # 代码内调用
    from mult_agents.rag.db_viewer import MilvusDBViewer
    viewer = MilvusDBViewer(host="127.0.0.1", port=19530)
    viewer.list_collections()
    viewer.show_child_chunks(limit=10)
    viewer.show_stats()
"""

import argparse
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

# 项目根目录
project_root = Path(__file__).resolve().parents[3]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from dotenv import load_dotenv
env_path = project_root / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)

logger = logging.getLogger("MilvusDBViewer")


class MilvusDBViewer:
    """Milvus 向量数据库查看器。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 19530,
        child_collection: str = "mult_agent_knowledge",
        parent_collection: str = "mult_agent_knowledge_parent",
    ):
        from pymilvus import connections, utility
        self._host = host
        self._port = port
        self._child_collection = child_collection
        self._parent_collection = parent_collection
        self._utility = utility

        # 连接 Milvus
        try:
            connections.connect(
                alias="default",
                host=host,
                port=port,
            )
            logger.info("已连接 Milvus: %s:%d", host, port)
        except Exception as e:
            logger.error("连接 Milvus 失败: %s", e)
            raise

    # ── 集合级别操作 ──────────────────────────────────────────

    def list_collections(self) -> List[str]:
        """列出所有集合名称。"""
        cols = self._utility.list_collections()
        print("\n" + "=" * 60)
        print("📋 Milvus 集合列表")
        print("=" * 60)
        for col in cols:
            row_count = 0
            try:
                from pymilvus import Collection
                c = Collection(col)
                row_count = c.num_entities
            except Exception:
                pass
            tag = ""
            if col == self._child_collection:
                tag = " ← 子块集合(精确检索)"
            elif col == self._parent_collection:
                tag = " ← 父块集合(上下文增强)"
            print(f"  • {col} | 行数={row_count}{tag}")
        print()
        return cols

    def show_schema(self, collection_name: Optional[str] = None):
        """查看集合的 schema 信息。"""
        from pymilvus import Collection

        targets = [collection_name] if collection_name else [
            self._child_collection, self._parent_collection
        ]
        for col_name in targets:
            if not self._utility.has_collection(col_name):
                print(f"⚠ 集合不存在: {col_name}")
                continue
            col = Collection(col_name)
            print("\n" + "=" * 60)
            print(f"📊 集合 Schema: {col_name}")
            print("=" * 60)
            print(f"  描述: {col.description}")
            print(f"  行数: {col.num_entities}")
            print(f"  主键: {col.schema.primary_field.name}")
            print("\n  字段列表:")
            for field in col.schema.fields:
                print(f"    - {field.name} | 类型={field.dtype} | "
                      f"主键={field.is_primary} | 自动ID={field.auto_id}")
            print("\n  索引列表:")
            for idx in col.indexes:
                print(f"    - {idx.field_name} | 类型={idx.params.get('index_type')} | "
                      f"metric={idx.params.get('metric_type')}")
            col.flush()

    def show_stats(self):
        """统计各集合和各文档的 chunk 数量。"""
        from pymilvus import Collection

        for col_name, label in [
            (self._child_collection, "子块集合(精确检索)"),
            (self._parent_collection, "父块集合(上下文增强)"),
        ]:
            if not self._utility.has_collection(col_name):
                print(f"⚠ 集合不存在: {col_name}")
                continue

            col = Collection(col_name)
            col.load()
            total = col.num_entities

            # 查询所有数据获取 source_name 统计
            try:
                results = col.query(
                    expr="pk >= 0",
                    output_fields=["source_name", "source", "chunk_type"],
                    limit=16384,
                )
                source_counter = Counter()
                for row in results:
                    sn = row.get("source_name") or row.get("source") or "unknown"
                    source_counter[sn] += 1
            except Exception as e:
                logger.warning("查询 source 统计失败: %s", e)
                source_counter = Counter()

            print("\n" + "=" * 60)
            print(f"📈 统计: {col_name} ({label})")
            print("=" * 60)
            print(f"  总行数: {total}")
            print(f"  文档数: {len(source_counter)}")
            if source_counter:
                print("\n  各文档 chunk 数:")
                for doc_name, count in source_counter.most_common():
                    print(f"    {doc_name}: {count} chunks")
            col.flush()

    # ── 数据查看操作 ──────────────────────────────────────────

    def show_child_chunks(
        self,
        limit: int = 10,
        filter_source: Optional[str] = None,
        show_vector: bool = False,
    ):
        """查看子块集合中的数据。"""
        self._show_chunks(
            collection_name=self._child_collection,
            label="子块集合",
            limit=limit,
            filter_source=filter_source,
            show_vector=show_vector,
        )

    def show_parent_chunks(
        self,
        limit: int = 5,
        filter_source: Optional[str] = None,
        show_vector: bool = False,
    ):
        """查看父块集合中的数据。"""
        self._show_chunks(
            collection_name=self._parent_collection,
            label="父块集合",
            limit=limit,
            filter_source=filter_source,
            show_vector=show_vector,
        )

    def _show_chunks(
        self,
        collection_name: str,
        label: str,
        limit: int = 10,
        filter_source: Optional[str] = None,
        show_vector: bool = False,
    ):
        """通用数据查看方法。"""
        from pymilvus import Collection

        if not self._utility.has_collection(collection_name):
            print(f"⚠ 集合不存在: {collection_name}")
            return

        col = Collection(collection_name)
        col.load()

        # 构建 filter 表达式
        expr = "pk >= 0"
        if filter_source:
            # 使用 source_name like 模糊匹配
            expr = f'source_name like "%{filter_source}%"'

        # 获取所有字段名
        field_names = [f.name for f in col.schema.fields]
        output_fields = [f for f in field_names if f != "vector"]
        if show_vector and "vector" in field_names:
            output_fields.append("vector")

        try:
            results = col.query(
                expr=expr,
                output_fields=output_fields,
                limit=limit,
            )
        except Exception as e:
            print(f"❌ 查询失败: {e}")
            return

        print("\n" + "=" * 60)
        print(f"📄 {label} 数据 (collection={collection_name}, limit={limit})")
        if filter_source:
            print(f"   过滤条件: source_name like '%{filter_source}%'")
        print("=" * 60)

        if not results:
            print("  (无数据)")
            return

        for i, row in enumerate(results, 1):
            print(f"\n--- 第 {i} 条 ---")
            for key, val in row.items():
                if key == "vector" and isinstance(val, list):
                    # 向量只显示前 5 维 + 维度
                    preview = [f"{v:.4f}" for v in val[:5]]
                    print(f"  {key}: [{', '.join(preview)}, ... ] (维度={len(val)})")
                elif key in ("text", "page_content"):
                    text_val = str(val)
                    if len(text_val) > 200:
                        text_val = text_val[:200] + "..."
                    print(f"  {key}: {text_val}")
                elif isinstance(val, dict):
                    print(f"  {key}: {json.dumps(val, ensure_ascii=False, indent=2)}")
                elif isinstance(val, str) and len(val) > 200:
                    print(f"  {key}: {val[:200]}...")
                else:
                    print(f"  {key}: {val}")

        col.flush()

    def search_by_query(
        self,
        query: str,
        collection_name: Optional[str] = None,
        top_k: int = 5,
    ):
        """通过向量相似度搜索查看最相似的数据。"""
        from pymilvus import Collection
        from langchain_community.embeddings import DashScopeEmbeddings

        api_key = os.getenv("DASHSCOPE_API_KEY", "")
        if not api_key:
            print("❌ 需要 DASHSCOPE_API_KEY 环境变量")
            return

        col_name = collection_name or self._child_collection
        if not self._utility.has_collection(col_name):
            print(f"⚠ 集合不存在: {col_name}")
            return

        # 生成查询向量
        embeddings = DashScopeEmbeddings(
            model="text-embedding-v1",
            dashscope_api_key=api_key,
        )
        query_vector = embeddings.embed_query(query)

        col = Collection(col_name)
        col.load()

        # 获取字段名
        field_names = [f.name for f in col.schema.fields]
        output_fields = [f for f in field_names if f != "vector"]

        # 执行向量搜索
        results = col.search(
            data=[query_vector],
            anns_field="vector",
            param={"metric_type": "COSINE", "params": {"nprobe": 10}},
            limit=top_k,
            output_fields=output_fields,
        )

        print("\n" + "=" * 60)
        print(f"🔍 向量搜索结果 (query='{query}', collection={col_name})")
        print("=" * 60)

        if not results or not results[0]:
            print("  (无结果)")
            return

        for i, hit in enumerate(results[0], 1):
            score = hit.score if hasattr(hit, "score") else hit.get("score", 0)
            entity = hit.entity if hasattr(hit, "entity") else hit.get("entity", {})
            print(f"\n--- 第 {i} 条 (相似度={score:.4f}) ---")
            for key in output_fields:
                val = entity.get(key) if hasattr(entity, "get") else getattr(entity, key, None)
                if val is None:
                    # 尝试从 fields_data 获取
                    val = hit.fields.get(key) if hasattr(hit, "fields") else None
                if key in ("text", "page_content") and val:
                    val_str = str(val)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "..."
                    print(f"  {key}: {val_str}")
                elif isinstance(val, dict):
                    print(f"  {key}: {json.dumps(val, ensure_ascii=False)}")
                elif isinstance(val, str) and len(str(val)) > 200:
                    print(f"  {key}: {str(val)[:200]}...")
                else:
                    print(f"  {key}: {val}")

        col.flush()


# ── 命令行入口 ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Milvus 向量数据库查看工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python -m mult_agents.rag.db_viewer --list-collections
  python -m mult_agents.rag.db_viewer --show-child --limit 10
  python -m mult_agents.rag.db_viewer --show-parent --limit 5
  python -m mult_agents.rag.db_viewer --filter-source "DeepResearch" --show-child
  python -m mult_agents.rag.db_viewer --stats
  python -m mult_agents.rag.db_viewer --schema
  python -m mult_agents.rag.db_viewer --search "RAG 切片策略"
        """,
    )
    parser.add_argument("--host", type=str, default=os.getenv("MILVUS_HOST", "127.0.0.1"),
                        help="Milvus host")
    parser.add_argument("--port", type=int, default=int(os.getenv("MILVUS_PORT", "19530")),
                        help="Milvus port")
    parser.add_argument("--child-collection", type=str, default="mult_agent_knowledge",
                        help="子块集合名")
    parser.add_argument("--parent-collection", type=str, default="mult_agent_knowledge_parent",
                        help="父块集合名")

    parser.add_argument("--list-collections", action="store_true",
                        help="列出所有集合")
    parser.add_argument("--show-child", action="store_true",
                        help="查看子块集合数据")
    parser.add_argument("--show-parent", action="store_true",
                        help="查看父块集合数据")
    parser.add_argument("--limit", type=int, default=10,
                        help="返回数据条数")
    parser.add_argument("--filter-source", type=str, default=None,
                        help="按 source_name 模糊过滤")
    parser.add_argument("--show-vector", action="store_true",
                        help="显示向量数据（前5维）")
    parser.add_argument("--stats", action="store_true",
                        help="查看统计信息")
    parser.add_argument("--schema", action="store_true",
                        help="查看集合 schema")
    parser.add_argument("--search", type=str, default=None,
                        help="向量搜索查询")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    )

    viewer = MilvusDBViewer(
        host=args.host,
        port=args.port,
        child_collection=args.child_collection,
        parent_collection=args.parent_collection,
    )

    if args.list_collections:
        viewer.list_collections()

    if args.schema:
        viewer.show_schema()

    if args.stats:
        viewer.show_stats()

    if args.show_child:
        viewer.show_child_chunks(
            limit=args.limit,
            filter_source=args.filter_source,
            show_vector=args.show_vector,
        )

    if args.show_parent:
        viewer.show_parent_chunks(
            limit=args.limit,
            filter_source=args.filter_source,
            show_vector=args.show_vector,
        )

    if args.search:
        viewer.search_by_query(query=args.search, top_k=args.limit)

    # 如果没有指定任何操作，默认列出集合
    if not any([
        args.list_collections, args.show_child, args.show_parent,
        args.stats, args.schema, args.search
    ]):
        viewer.list_collections()


if __name__ == "__main__":
    main()
