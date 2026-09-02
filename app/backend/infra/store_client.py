"""P5-1: PostgresStore 单例 — LangGraph 原生 BaseStore，独立 schema 物理隔离。

设计要点：
  1. 使用 AsyncPostgresStore（异步连接池），与 checkpointer 表族物理隔离
     —— PostgresStore 自带 store_migrations / vector_migrations 表，在公共 schema 建表
  2. embedding 用 DashScope text-embedding-v3（1024 维），与 Milvus RAG 链路同源
  3. lifespan 启动时 await store.setup() 建表 + 向量索引
  4. 全局单例，供 memory_service / research_service / router 共享

参考: memory-template:src/chatbot/graph.py:31-36（namespace + store.asearch）
"""

import logging
from typing import Optional

from langgraph.store.postgres import AsyncPostgresStore
from langgraph.store.postgres.base import PostgresIndexConfig
from langchain_community.embeddings import DashScopeEmbeddings

logger = logging.getLogger("backend.infra.store")

# ── 全局单例 ──────────────────────────────────────

_store_instance: Optional[AsyncPostgresStore] = None
_store_context = None  # AsyncPostgresStore.from_conn_string 的 context manager


async def init_store(
    postgres_dsn: str,
    dashscope_api_key: str,
    embedding_model: str = "text-embedding-v3",
) -> AsyncPostgresStore:
    """初始化 PostgresStore 单例（lifespan 启动时调用）。

    Args:
        postgres_dsn: PostgreSQL 连接串
        dashscope_api_key: DashScope API Key（用于 embedding）
        embedding_model: embedding 模型名（默认 text-embedding-v3，1024 维）

    Returns:
        AsyncPostgresStore 实例
    """
    global _store_instance, _store_context

    if _store_instance is not None:
        return _store_instance

    # 构建 DashScope embedding 实例
    embeddings = DashScopeEmbeddings(
        model=embedding_model,
        dashscope_api_key=dashscope_api_key,
    )

    # index 配置：指定维度 + embedding 函数 + 索引字段
    index_config: PostgresIndexConfig = {
        "dims": 1024,  # text-embedding-v3 输出 1024 维
        "embed": embeddings,
        "fields": ["text"],  # 对 value["text"] 字段做向量索引
        "distance_type": "cosine",
    }

    # 使用连接池模式
    from psycopg_pool import AsyncConnectionPool

    _store_context = AsyncPostgresStore.from_conn_string(
        postgres_dsn,
        pool_config={"min_size": 2, "max_size": 10},
        index=index_config,
    )

    _store_instance = await _store_context.__aenter__()
    await _store_instance.setup()

    logger.info(
        "PostgresStore 初始化完成 | embedding=%s | dims=1024 | fields=['text']",
        embedding_model,
    )
    return _store_instance


async def close_store() -> None:
    """关闭 PostgresStore 连接池（lifespan shutdown 时调用）。"""
    global _store_instance, _store_context
    if _store_context is not None:
        try:
            await _store_context.__aexit__(None, None, None)
            logger.info("PostgresStore 连接已关闭")
        except Exception as exc:
            logger.warning("PostgresStore 关闭失败: %s", exc)
    _store_instance = None
    _store_context = None


def get_store() -> Optional[AsyncPostgresStore]:
    """获取当前 PostgresStore 单例（未初始化时返回 None）。"""
    return _store_instance
