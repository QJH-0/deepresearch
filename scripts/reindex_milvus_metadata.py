#!/usr/bin/env python
"""重索引存量 Milvus 数据：从 PG 的 document_chunks 补回切片 metadata。

## 背景

存量向量是用旧写入路径灌进去的，Milvus 动态字段为空 —— 经 langchain 接口读回
`metadata` 只有 `pk`，缺少 `doc_id / source / section_path / parent_id / child_id`。
后果：

- `RAGSystem.search_records` 里 `doc_id` 与 `section_path` 为空 → 本地来源无法定位，
  引用列表只能退化为「本地知识片段-N」
- `doc.metadata.get("parent_id")` 恒为空 → 父子上下文扩展对存量数据完全不触发

PG 的 `document_chunks` 里完整保存着这些字段，因此可以直接回灌 Milvus，
**不修改任何 PG 数据**。

## 已知限制

父块的原始文本没有落库（`document_chunks` 只存子块），无法还原。
本脚本按现有运行时行为，用该父块下第一个子块的内容作为父块文本，
即**只修复 metadata，不改变父块文本**。若需要正确的父块上下文，
需重新上传原文档（MinIO 里仍保留原始文件）。

## 用法

    python -m scripts.reindex_milvus_metadata             # dry-run，只报告不写入
    python -m scripts.reindex_milvus_metadata --apply      # 实际写入
    python -m scripts.reindex_milvus_metadata --apply --doc-id <doc_id>   # 只处理单个文档
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "app"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_PROJECT_ROOT / ".env")

import psycopg  # noqa: E402
from langchain_core.documents import Document  # noqa: E402

from mult_agents.config import AppConfig  # noqa: E402
from mult_agents.rag.core import (  # noqa: E402
    DEFAULT_CHILD_COLLECTION,
    DEFAULT_PARENT_COLLECTION,
    RAGConfig,
    RAGSystem,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("reindex")

# 由 main() 依据 AppConfig 填充，供扫描/清理辅助函数使用
_MILVUS_HOST = "127.0.0.1"
_MILVUS_PORT = 19530


def _load_indexed_chunks(dsn: str, only_doc_id: str = "") -> dict:
    """读取已向量化的切片，按 doc_id 分组。"""
    sql = """
        SELECT c.doc_id, c.parent_id, c.chunk_idx, c.content, c.section_path,
               c.metadata, d.filename
        FROM document_chunks c
        JOIN documents d ON d.id = c.doc_id
        WHERE c.vector_status = 'indexed'
    """
    params: tuple = ()
    if only_doc_id:
        sql += " AND c.doc_id = %s"
        params = (only_doc_id,)
    sql += " ORDER BY c.doc_id, c.parent_id, c.chunk_idx"

    grouped: dict = defaultdict(list)
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        for doc_id, parent_id, chunk_idx, content, section_path, metadata, filename in cur:
            grouped[doc_id].append({
                "parent_id": parent_id or "",
                "chunk_idx": chunk_idx or 0,
                "content": content or "",
                "section_path": section_path or "",
                "metadata": metadata or {},
                "filename": filename or "",
            })
    return grouped


def _build_documents(doc_id: str, rows: list) -> tuple[list, list]:
    """把 PG 行还原成 Milvus 需要的子块与父块 Document 列表。

    字段与 chunk_consumer._process_message 写入时保持一致，
    保证重索引后的数据形态与正常运行时的写入结果同构。
    """
    children: list = []
    parents_by_id: dict = {}

    for row in rows:
        meta = row["metadata"] or {}
        source_name = meta.get("source_name") or row["filename"]
        child_doc = Document(
            page_content=row["content"],
            metadata={
                "source": doc_id,
                "source_name": source_name,
                "section_path": row["section_path"],
                "parent_id": row["parent_id"],
                "child_id": meta.get("child_id", ""),
                "chunk_type": "child",
                "chunk_idx": row["chunk_idx"],
                "doc_id": doc_id,
            },
        )
        children.append(child_doc)

        parent_id = row["parent_id"]
        if parent_id and parent_id not in parents_by_id:
            parents_by_id[parent_id] = Document(
                page_content=row["content"],
                metadata={
                    "source": doc_id,
                    "source_name": source_name,
                    "section_path": row["section_path"],
                    "parent_id": parent_id,
                    "chunk_type": "parent",
                    "doc_id": doc_id,
                },
            )

    return children, list(parents_by_id.values())


def _scan_legacy_rows(collection_name: str) -> tuple[int, int, list]:
    """扫描集合，返回 (总行数, 缺 doc_id 的存量行数, 存量行 pk 列表)。

    缺 doc_id 的行是旧写入路径留下的：无法归属到任何文档、也无法被引用，
    且按 doc_id 删除时匹配不到它们 —— 必须先清掉，否则重索引会在库里留下重复内容。
    """
    from pymilvus import Collection, DataType, connections

    connections.connect(alias="reindex", host=_MILVUS_HOST, port=_MILVUS_PORT)
    col = Collection(collection_name, using="reindex")
    col.load()

    pk_field = col.schema.primary_field
    expr_all = (
        f"{pk_field.name} >= 0"
        if pk_field.dtype == DataType.INT64
        else f'{pk_field.name} != ""'
    )
    rows = col.query(expr=expr_all, output_fields=[pk_field.name, "doc_id"], limit=16384)
    legacy = [r[pk_field.name] for r in rows if not r.get("doc_id")]
    return len(rows), len(legacy), legacy


def _purge_legacy_rows(collection_name: str) -> int:
    """删除缺 doc_id 的存量行，返回删除条数。"""
    from pymilvus import Collection, DataType, connections

    connections.connect(alias="reindex", host=_MILVUS_HOST, port=_MILVUS_PORT)
    total, _legacy_count, legacy_pks = _scan_legacy_rows(collection_name)
    if not legacy_pks:
        return 0

    col = Collection(collection_name, using="reindex")
    pk_name = col.schema.primary_field.name
    # pymilvus 的 Collection.delete 只接受表达式（没有 ids 参数），
    # 按主键删除需拼成 pk in [...]，VarChar 主键的值要加引号
    if col.schema.primary_field.dtype == DataType.INT64:
        values = ", ".join(str(int(v)) for v in legacy_pks)
    else:
        values = ", ".join(f'"{v}"' for v in legacy_pks)
    col.delete(expr=f"{pk_name} in [{values}]")
    col.flush()
    logger.info("清理存量行 | %s | 删除 %d / %d", collection_name, len(legacy_pks), total)
    return len(legacy_pks)


def main() -> int:
    parser = argparse.ArgumentParser(description="重索引存量 Milvus 切片的 metadata")
    parser.add_argument("--apply", action="store_true", help="实际写入；缺省为 dry-run")
    parser.add_argument("--doc-id", default="", help="只处理指定文档")
    args = parser.parse_args()

    config = AppConfig.from_file()
    grouped = _load_indexed_chunks(config.postgres_dsn, args.doc_id)

    global _MILVUS_HOST, _MILVUS_PORT
    _MILVUS_HOST = config.milvus_host
    _MILVUS_PORT = config.milvus_port

    if not grouped:
        logger.info("没有 vector_status='indexed' 的切片，无需重索引")
        return 0

    total_children = sum(len(v) for v in grouped.values())
    logger.info("PG 侧待重索引：%d 个文档 / %d 条子块", len(grouped), total_children)

    for doc_id, rows in grouped.items():
        parent_ids = {r["parent_id"] for r in rows if r["parent_id"]}
        logger.info(
            "  待处理 doc=%s | 子块=%d | 父块=%d | filename=%s",
            doc_id, len(rows), len(parent_ids), rows[0]["filename"],
        )

    if not args.apply:
        for name in (DEFAULT_CHILD_COLLECTION, DEFAULT_PARENT_COLLECTION):
            try:
                total, legacy, _ = _scan_legacy_rows(name)
            except Exception as exc:
                logger.warning("[dry-run] 无法扫描 %s | %s", name, exc)
                continue
            logger.info(
                "[dry-run] %s | 现有 %d 行 | 其中缺 metadata 的存量行 %d 条将被清理",
                name, total, legacy,
            )
        logger.info("[dry-run] 未写入任何数据。确认无误后加 --apply 执行。")
        return 0

    # 1) 清理存量行（无法归属文档，且会与重索引结果重复）
    for name in (DEFAULT_CHILD_COLLECTION, DEFAULT_PARENT_COLLECTION):
        try:
            _purge_legacy_rows(name)
        except Exception as exc:
            logger.error("清理存量行失败 | %s | %s", name, exc, exc_info=True)
            return 1

    # 2) 从 PG 回灌 metadata
    rag = RAGSystem(
        api_key=config.api_key,
        config=RAGConfig(
            milvus_host=config.milvus_host,
            milvus_port=config.milvus_port,
            postgres_dsn=config.postgres_dsn,
        ),
    )

    ok, failed = 0, 0
    for doc_id, rows in grouped.items():
        children, parents = _build_documents(doc_id, rows)
        try:
            if children:
                rag.vectorstore.add_documents(children)
                rag.bm25.add_documents(children)
            if parents:
                rag.parent_store.add_documents(parents)

            ok += 1
            logger.info(
                "重索引完成 | doc=%s | 子块=%d | 父块=%d | filename=%s",
                doc_id, len(children), len(parents), rows[0]["filename"],
            )
        except Exception as exc:
            failed += 1
            logger.error("重索引失败 | doc=%s | %s", doc_id, exc, exc_info=True)

    logger.info("汇总：成功 %d 个文档，失败 %d 个", ok, failed)
    if failed:
        logger.warning("存在失败项，可对同一命令重跑（脚本幂等：会先清理该文档的旧切片）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
