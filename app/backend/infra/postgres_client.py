"""
PostgreSQL 事务管理 + chunk 元数据持久化。

参考:
  - RAGFlow: 使用 MySQL 存储文档/chunk 元数据，与向量索引分离
  - Dify: 文档元数据存储在关系型 DB，向量存储在向量 DB
  - 本地消息表模式（Outbox Pattern）: 业务操作和消息记录在同一个事务中

设计要点:
  1. documents 表: 文档元信息（object_key, filename, status, chunk_count）
  2. document_chunks 表: 切块元数据（content, parent_id, chunk_idx, vector_status）
  3. chunk_sync_messages 表: 本地消息表（Outbox Pattern），保证事务一致性
  4. vector_status 字段: pending → indexed / failed，追踪向量化进度
  5. 全部 DDL 使用 IF NOT EXISTS，幂等创建
"""

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, List, Optional

import psycopg
from psycopg_pool import ConnectionPool

logger = logging.getLogger("backend.infra.postgres")

# ── DDL: 表结构定义 ──────────────────────────────────────────────────

DDL_DOCUMENTS = """
CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    object_key      TEXT NOT NULL,
    filename        TEXT NOT NULL,
    file_ext        TEXT NOT NULL DEFAULT '',
    file_size       BIGINT NOT NULL DEFAULT 0,
    content_hash    TEXT NOT NULL DEFAULT '',
    user_id         TEXT NOT NULL DEFAULT 'default_user',
    status          TEXT NOT NULL DEFAULT 'parsed',
    chunk_count     INT  NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_documents_user
    ON documents (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_documents_status
    ON documents (status);
"""

DDL_DOCUMENT_CHUNKS = """
CREATE TABLE IF NOT EXISTS document_chunks (
    id              TEXT PRIMARY KEY,
    doc_id          TEXT NOT NULL,
    parent_id       TEXT NOT NULL DEFAULT '',
    chunk_idx       INT  NOT NULL DEFAULT 0,
    content         TEXT NOT NULL,
    content_hash    TEXT NOT NULL DEFAULT '',
    section_path    TEXT NOT NULL DEFAULT '',
    metadata        JSONB NOT NULL DEFAULT '{}',
    vector_status   TEXT NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    FOREIGN KEY (doc_id) REFERENCES documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc
    ON document_chunks (doc_id, chunk_idx);
CREATE INDEX IF NOT EXISTS idx_chunks_parent
    ON document_chunks (parent_id);
CREATE INDEX IF NOT EXISTS idx_chunks_vector_status
    ON document_chunks (vector_status);
CREATE INDEX IF NOT EXISTS idx_chunks_content_hash
    ON document_chunks (content_hash);
"""

DDL_CHUNK_SYNC_MESSAGES = """
CREATE TABLE IF NOT EXISTS chunk_sync_messages (
    id              TEXT PRIMARY KEY,
    doc_id          TEXT NOT NULL,
    chunk_id        TEXT NOT NULL,
    payload         JSONB NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    retry_count     INT  NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_sync_messages_status
    ON chunk_sync_messages (status, created_at);
CREATE INDEX IF NOT EXISTS idx_sync_messages_doc
    ON chunk_sync_messages (doc_id);
"""

# 会话元数据表（行业通用做法：conversation record 独立于 checkpointer）
#
# 为什么需要这张表:
#   LangGraph 的 checkpoints 表是为「状态恢复」设计的，不是为「会话列表」设计的:
#     - 没有 user_id 列，无法按用户隔离
#     - checkpoint_id 是 UUID，ORDER BY checkpoint_id DESC 得不到时间倒序
#     - 没有标题/置顶/消息数等展示字段
#   直接扫 checkpoints 会导致: 新建会话后历史列表乱序、找不到旧会话（用户反馈的问题）。
DDL_CHAT_THREADS = """
CREATE TABLE IF NOT EXISTS chat_threads (
    thread_id       TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL DEFAULT 'default_user',
    title           TEXT NOT NULL DEFAULT '',
    intent          TEXT NOT NULL DEFAULT '',
    message_count   INT  NOT NULL DEFAULT 0,
    completed       BOOLEAN NOT NULL DEFAULT FALSE,
    pinned          BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_chat_threads_user
    ON chat_threads (user_id, updated_at DESC);
"""


def ensure_tables(dsn: str) -> None:
    """幂等创建所有表（如不存在）。"""
    conn = psycopg.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(DDL_DOCUMENTS)
            cur.execute(DDL_DOCUMENT_CHUNKS)
            cur.execute(DDL_CHUNK_SYNC_MESSAGES)
            cur.execute(DDL_CHAT_THREADS)
        logger.info(
            "PG 表结构初始化完成 (documents, document_chunks, chunk_sync_messages, chat_threads)"
        )
    finally:
        conn.close()


@dataclass
class DocumentRecord:
    """documents 表行映射。"""
    id: str
    object_key: str
    filename: str
    file_ext: str = ""
    file_size: int = 0
    content_hash: str = ""
    user_id: str = "default_user"
    status: str = "parsed"
    chunk_count: int = 0


@dataclass
class ChunkRecord:
    """document_chunks 表行映射。"""
    id: str
    doc_id: str
    parent_id: str = ""
    chunk_idx: int = 0
    content: str = ""
    content_hash: str = ""
    section_path: str = ""
    metadata: dict = field(default_factory=dict)
    vector_status: str = "pending"


class ChunkRepository:
    """
    文档与 chunk 的持久化仓库。

    核心方法 insert_document_with_chunks 使用 PG 本地事务:
      - 插入 document 记录
      - 批量插入 chunk 记录
      - 批量插入 chunk_sync_messages（本地消息表 / Outbox Pattern）
      - 全部在同一个事务中，保证原子性
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: Optional[ConnectionPool] = None

    def _get_pool(self) -> ConnectionPool:
        if self._pool is None:
            self._pool = ConnectionPool(
                conninfo=self._dsn,
                min_size=2,
                max_size=10,
                timeout=30,
            )
        return self._pool

    def insert_document_with_chunks(
        self,
        doc: DocumentRecord,
        chunks: List[ChunkRecord],
    ) -> List[str]:
        """
        在一个 PG 本地事务中写入 document + chunks + sync_messages。

        返回: 新创建的 message_id 列表（用于后续 MQ 发送确认）。
        """
        pool = self._get_pool()
        message_ids: List[str] = []
        with pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    # 1. 插入 document
                    cur.execute(
                        """
                        INSERT INTO documents
                            (id, object_key, filename, file_ext, file_size,
                             content_hash, user_id, status, chunk_count)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            status = EXCLUDED.status,
                            chunk_count = EXCLUDED.chunk_count,
                            updated_at = NOW()
                        """,
                        (
                            doc.id, doc.object_key, doc.filename, doc.file_ext,
                            doc.file_size, doc.content_hash, doc.user_id,
                            doc.status, doc.chunk_count,
                        ),
                    )

                    # 2. 批量插入 chunks
                    chunk_rows = [
                        (
                            c.id, c.doc_id, c.parent_id, c.chunk_idx,
                            c.content, c.content_hash, c.section_path,
                            json.dumps(c.metadata, ensure_ascii=False),
                            c.vector_status,
                        )
                        for c in chunks
                    ]
                    cur.executemany(
                        """
                        INSERT INTO document_chunks
                            (id, doc_id, parent_id, chunk_idx, content,
                             content_hash, section_path, metadata, vector_status)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            content = EXCLUDED.content,
                            vector_status = EXCLUDED.vector_status
                        """,
                        chunk_rows,
                    )

                    # 3. 批量插入本地消息表 (Outbox Pattern)
                    msg_rows = []
                    for chunk in chunks:
                        msg_id = uuid.uuid4().hex
                        message_ids.append(msg_id)
                        payload = json.dumps({
                            "doc_id": chunk.doc_id,
                            "chunk_id": chunk.id,
                            "parent_id": chunk.parent_id,
                            "chunk_idx": chunk.chunk_idx,
                            "content": chunk.content,
                            "content_hash": chunk.content_hash,
                            "section_path": chunk.section_path,
                            "metadata": chunk.metadata,
                            "source_name": doc.filename,
                        }, ensure_ascii=False)
                        msg_rows.append((msg_id, chunk.doc_id, chunk.id, payload))

                    cur.executemany(
                        """
                        INSERT INTO chunk_sync_messages
                            (id, doc_id, chunk_id, payload, status)
                        VALUES (%s, %s, %s, %s, 'pending')
                        """,
                        msg_rows,
                    )

        logger.info(
            "PG 事务完成 | doc_id=%s | chunks=%d | messages=%d",
            doc.id, len(chunks), len(message_ids),
        )
        return message_ids

    def mark_messages_sent(self, message_ids: List[str]) -> None:
        """标记本地消息为已发送到 MQ。"""
        if not message_ids:
            return
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    UPDATE chunk_sync_messages
                    SET status = 'sent', sent_at = NOW()
                    WHERE id = %s
                    """,
                    [(mid,) for mid in message_ids],
                )

    def update_chunk_vector_status(
        self,
        chunk_id: str,
        status: str,
        milvus_pk: str = "",
    ) -> None:
        """
        更新 chunk 的向量化状态。

        status: 'indexed' (成功) / 'failed' (失败)
        milvus_pk: Milvus 中的主键 ID（存入 metadata 便于后续删除）
        """
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                metadata_update = json.dumps({"milvus_pk": milvus_pk}) if milvus_pk else None
                if metadata_update:
                    cur.execute(
                        """
                        UPDATE document_chunks
                        SET vector_status = %s,
                            metadata = metadata || %s
                        WHERE id = %s
                        """,
                        (status, metadata_update, chunk_id),
                    )
                else:
                    cur.execute(
                        "UPDATE document_chunks SET vector_status = %s WHERE id = %s",
                        (status, chunk_id),
                    )

    def get_pending_messages(self, limit: int = 100) -> List[dict]:
        """获取未发送到 MQ 的本地消息（用于补偿/重试）。"""
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, doc_id, chunk_id, payload, retry_count
                    FROM chunk_sync_messages
                    WHERE status = 'pending'
                    ORDER BY created_at ASC
                    LIMIT %s
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
                return [
                    {
                        "id": row[0],
                        "doc_id": row[1],
                        "chunk_id": row[2],
                        "payload": row[3],
                        "retry_count": row[4],
                    }
                    for row in rows
                ]

    def get_document_status(self, doc_id: str) -> dict:
        """查询文档状态和向量化进度。"""
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, filename, status, chunk_count FROM documents WHERE id = %s",
                    (doc_id,),
                )
                row = cur.fetchone()
                if not row:
                    return {}
                cur.execute(
                    """
                    SELECT vector_status, COUNT(*)
                    FROM document_chunks
                    WHERE doc_id = %s
                    GROUP BY vector_status
                    """,
                    (doc_id,),
                )
                status_counts = {r[0]: r[1] for r in cur.fetchall()}
                return {
                    "doc_id": row[0],
                    "filename": row[1],
                    "status": row[2],
                    "chunk_count": row[3],
                    "vector_status": status_counts,
                }

    def list_documents(self, user_id: str = "default_user", keyword: str = "") -> List[dict]:
        """
        列出用户文档，并一次性聚合出向量化进度。

        之前前端要判断「是否已进入向量库」，必须对每个文档再调一次
        GET /documents/status/{doc_id}，N 个文档就是 N+1 次请求，列表页会闪。
        这里改成单次 LEFT JOIN 聚合，直接返回 indexed/pending/failed 与 progress。
        """
        pool = self._get_pool()
        kw = f"%{keyword.strip()}%" if keyword.strip() else ""
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.id, d.object_key, d.filename, d.file_ext, d.file_size,
                           d.content_hash, d.user_id, d.status, d.chunk_count, d.created_at,
                           COALESCE(v.indexed_cnt, 0) AS indexed_cnt,
                           COALESCE(v.pending_cnt, 0) AS pending_cnt,
                           COALESCE(v.failed_cnt, 0)  AS failed_cnt
                    FROM documents d
                    LEFT JOIN (
                        SELECT doc_id,
                               COUNT(*) FILTER (WHERE vector_status = 'indexed') AS indexed_cnt,
                               COUNT(*) FILTER (WHERE vector_status = 'pending') AS pending_cnt,
                               COUNT(*) FILTER (WHERE vector_status = 'failed')  AS failed_cnt
                        FROM document_chunks
                        GROUP BY doc_id
                    ) v ON v.doc_id = d.id
                    WHERE d.user_id = %s
                      AND (%s = '' OR d.filename ILIKE %s)
                    ORDER BY d.created_at DESC
                    """,
                    (user_id, kw, kw),
                )
                rows = cur.fetchall()
                return [self._doc_row_to_dict(r) for r in rows]

    @staticmethod
    def _doc_row_to_dict(r) -> dict:
        """把文档行（含向量化计数）转成前端展示用的 dict。"""
        chunk_count = r[8] or 0
        indexed = r[10] or 0
        pending = r[11] or 0
        failed = r[12] or 0
        progress = round(indexed / chunk_count * 100) if chunk_count > 0 else 0
        # 文档级状态以 chunk 实际向量化结果为准，而不是 documents.status 的静态值
        if chunk_count == 0:
            vector_state = "empty"
        elif failed > 0 and pending == 0 and indexed == 0:
            vector_state = "failed"
        elif indexed >= chunk_count:
            vector_state = "indexed"
        elif failed > 0:
            vector_state = "partial"
        else:
            vector_state = "processing"
        return {
            "doc_id": r[0],
            "object_key": r[1],
            "filename": r[2],
            "file_ext": r[3],
            "file_size": r[4],
            "content_hash": r[5],
            "user_id": r[6],
            "status": r[7],
            "chunk_count": chunk_count,
            "created_at": str(r[9]),
            "indexed_chunks": indexed,
            "pending_chunks": pending,
            "failed_chunks": failed,
            "progress": progress,
            "vector_state": vector_state,
        }

    def get_documents_stats(self, user_id: str = "default_user") -> dict:
        """知识库总览统计：文档数 / 切片数 / 已索引 / 待处理 / 失败。"""
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        COUNT(*)                                        AS doc_total,
                        COALESCE(SUM(chunk_count), 0)                   AS chunk_total,
                        COALESCE(SUM(file_size), 0)                     AS size_total,
                        COUNT(*) FILTER (WHERE status = 'failed')       AS doc_failed
                    FROM documents
                    WHERE user_id = %s
                    """,
                    (user_id,),
                )
                doc_row = cur.fetchone()
                cur.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE c.vector_status = 'indexed') AS indexed,
                        COUNT(*) FILTER (WHERE c.vector_status = 'pending') AS pending,
                        COUNT(*) FILTER (WHERE c.vector_status = 'failed')  AS failed
                    FROM document_chunks c
                    JOIN documents d ON d.id = c.doc_id
                    WHERE d.user_id = %s
                    """,
                    (user_id,),
                )
                chunk_row = cur.fetchone()

        doc_total = doc_row[0] or 0
        chunk_total = doc_row[1] or 0
        size_total = doc_row[2] or 0
        doc_failed = doc_row[3] or 0
        indexed = chunk_row[0] or 0
        pending = chunk_row[1] or 0
        failed = chunk_row[2] or 0
        return {
            "document_count": doc_total,
            "chunk_count": chunk_total,
            "size_bytes": size_total,
            "indexed_chunks": indexed,
            "pending_chunks": pending,
            "failed_chunks": failed,
            "failed_documents": doc_failed,
            "ready_documents": 0 if doc_total == 0 else self._count_ready_documents(user_id),
            "progress": round(indexed / chunk_total * 100) if chunk_total > 0 else 0,
        }

    def _count_ready_documents(self, user_id: str) -> int:
        """统计「全部切片都已进向量库」的文档数（即可被检索的文档数）。"""
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM (
                        SELECT d.id
                        FROM documents d
                        JOIN document_chunks c ON c.doc_id = d.id
                        WHERE d.user_id = %s AND d.chunk_count > 0
                        GROUP BY d.id, d.chunk_count
                        HAVING COUNT(*) FILTER (WHERE c.vector_status = 'indexed') >= d.chunk_count
                    ) ready
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
        return row[0] if row else 0

    def retry_failed_chunks(self, doc_id: str) -> List[str]:
        """
        重试未完成的切片：把 failed/pending chunk 重置并重建 Outbox 消息。

        返回新生成的 message payload 列表（由调用方直接投递 MQ，
        不依赖只在启动时跑一次的补偿扫描）。
        """
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE document_chunks
                        SET vector_status = 'pending'
                        WHERE doc_id = %s AND vector_status IN ('failed', 'pending')
                        """,
                        (doc_id,),
                    )
                    reset_count = cur.rowcount or 0
                    if reset_count == 0:
                        return []

                    cur.execute(
                        """
                        SELECT c.id, c.parent_id, c.chunk_idx, c.content,
                               c.content_hash, c.section_path, c.metadata, d.filename
                        FROM document_chunks c
                        JOIN documents d ON d.id = c.doc_id
                        WHERE c.doc_id = %s AND c.vector_status = 'pending'
                        """,
                        (doc_id,),
                    )
                    rows = cur.fetchall()

                    # 清掉该文档残留的旧消息，避免重复投递
                    cur.execute(
                        "DELETE FROM chunk_sync_messages WHERE doc_id = %s",
                        (doc_id,),
                    )

                    msg_rows = []
                    for row in rows:
                        msg_id = uuid.uuid4().hex
                        payload = json.dumps({
                            "doc_id": doc_id,
                            "chunk_id": row[0],
                            "parent_id": row[1],
                            "chunk_idx": row[2],
                            "content": row[3],
                            "content_hash": row[4],
                            "section_path": row[5],
                            "metadata": row[6] or {},
                            "source_name": row[7],
                        }, ensure_ascii=False)
                        msg_rows.append((msg_id, doc_id, row[0], payload))

                    cur.executemany(
                        """
                        INSERT INTO chunk_sync_messages
                            (id, doc_id, chunk_id, payload, status)
                        VALUES (%s, %s, %s, %s, 'pending')
                        """,
                        msg_rows,
                    )
        logger.info("重试向量化 | doc_id=%s | 重置切片=%d", doc_id, len(msg_rows))
        return [m[3] for m in msg_rows]

    def delete_documents_batch(self, doc_ids: List[str], user_id: str) -> List[str]:
        """
        批量删除文档，返回被删文档的 object_key 列表（供 MinIO 清理）。

        只删 user_id 名下的文档，避免越权删除他人数据。
        """
        if not doc_ids:
            return []
        pool = self._get_pool()
        object_keys: List[str] = []
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, object_key FROM documents
                    WHERE id = ANY(%s) AND user_id = %s
                    """,
                    (list(doc_ids), user_id),
                )
                rows = cur.fetchall()
                if not rows:
                    return []
                object_keys = [r[1] for r in rows]
                cur.execute(
                    "DELETE FROM documents WHERE id = ANY(%s) AND user_id = %s",
                    ([r[0] for r in rows], user_id),
                )
        return object_keys

    def delete_document(self, doc_id: str) -> Optional[str]:
        """
        删除文档（级联删除 chunks 和 sync_messages）。
        返回 object_key（用于 MinIO 清理），不存在返回 None。
        """
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT object_key FROM documents WHERE id = %s",
                    (doc_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                object_key = row[0]
                cur.execute("DELETE FROM documents WHERE id = %s", (doc_id,))
                # chunks 和 sync_messages 通过 ON DELETE CASCADE 自动删除
        return object_key


# ── 会话元数据仓储 ────────────────────────────────────────────────────

def generate_thread_title(query: str, max_len: int = 50) -> str:
    """
    从首条用户提问自动生成会话标题（行业通用做法）。

    压缩空白后截断，超长补省略号，保证侧边栏一行内可扫读。
    """
    import re

    text = re.sub(r"\s+", " ", (query or "").strip())
    if not text:
        return "新会话"
    return text if len(text) <= max_len else text[:max_len].rstrip() + "…"


class ThreadRepository:
    """
    会话（thread）元数据仓储。

    独立维护 chat_threads 表，而不是去扫 LangGraph 的 checkpoints 表:
      - checkpoints 没有 user_id，无法隔离用户
      - checkpoint_id 是 UUID，ORDER BY 它得不到时间倒序
        （这正是「新建会话后找不到旧对话」的根因）
      - 标题/置顶/消息数这些展示字段在 checkpoints 里没有落点
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: Optional[ConnectionPool] = None

    def _get_pool(self) -> ConnectionPool:
        if self._pool is None:
            self._pool = ConnectionPool(
                conninfo=self._dsn,
                min_size=1,
                max_size=5,
                timeout=30,
            )
        return self._pool

    def upsert_thread(
        self,
        thread_id: str,
        user_id: str = "default_user",
        title: str = "",
        intent: str = "",
        completed: bool = False,
        message_delta: int = 0,
    ) -> None:
        """
        创建或更新会话记录。

        title 留空时不覆盖已有标题（首次写入用提问自动命名，
        之后用户手动重命名过的标题不能被后续轮次冲掉）。
        """
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO chat_threads
                        (thread_id, user_id, title, intent, message_count, completed)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (thread_id) DO UPDATE SET
                        user_id       = EXCLUDED.user_id,
                        title         = CASE
                            WHEN EXCLUDED.title <> '' THEN EXCLUDED.title
                            ELSE chat_threads.title
                        END,
                        intent        = CASE
                            WHEN EXCLUDED.intent <> '' THEN EXCLUDED.intent
                            ELSE chat_threads.intent
                        END,
                        message_count = chat_threads.message_count + %s,
                        completed     = chat_threads.completed OR EXCLUDED.completed,
                        updated_at    = NOW()
                    """,
                    (
                        thread_id, user_id, title, intent,
                        max(1, message_delta), completed, message_delta,
                    ),
                )

    def touch_thread(self, thread_id: str, message_delta: int = 0) -> None:
        """仅刷新活跃时间 / 累加消息数。"""
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE chat_threads
                    SET message_count = message_count + %s,
                        updated_at = NOW()
                    WHERE thread_id = %s
                    """,
                    (message_delta, thread_id),
                )

    def mark_completed(self, thread_id: str, intent: str = "") -> None:
        """标记会话已产出最终结果。"""
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE chat_threads
                    SET completed = TRUE,
                        intent = CASE WHEN %s <> '' THEN %s ELSE intent END,
                        updated_at = NOW()
                    WHERE thread_id = %s
                    """,
                    (intent, intent, thread_id),
                )

    def list_threads(
        self,
        user_id: str = "default_user",
        limit: int = 50,
        keyword: str = "",
    ) -> List[dict]:
        """
        列出会话：置顶优先，其余按最近活跃时间倒序。

        这正是用户期望的顺序 —— 新建会话排最前，旧会话依次下移，
        而不是像扫 checkpoints 那样按 thread_id 字符串排序。
        """
        pool = self._get_pool()
        kw = f"%{keyword.strip()}%" if keyword.strip() else ""
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT thread_id, title, intent, message_count,
                           completed, pinned, created_at, updated_at
                    FROM chat_threads
                    WHERE user_id = %s
                      AND (%s = '' OR title ILIKE %s)
                    ORDER BY pinned DESC, updated_at DESC
                    LIMIT %s
                    """,
                    (user_id, kw, kw, max(1, min(limit, 200))),
                )
                rows = cur.fetchall()
        return [
            {
                "thread_id": r[0],
                "query": r[1] or "新会话",
                "title": r[1] or "新会话",
                "intent": r[2] or "",
                "message_count": r[3] or 0,
                "completed": bool(r[4]),
                "pinned": bool(r[5]),
                "created_at": r[6].isoformat() if r[6] else "",
                "updated_at": r[7].isoformat() if r[7] else "",
            }
            for r in rows
        ]

    def rename_thread(self, thread_id: str, title: str, user_id: str) -> bool:
        """重命名会话（校验归属，防止越权改他人会话）。"""
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE chat_threads
                    SET title = %s, updated_at = NOW()
                    WHERE thread_id = %s AND user_id = %s
                    """,
                    (title.strip() or "新会话", thread_id, user_id),
                )
                return (cur.rowcount or 0) > 0

    def set_pinned(self, thread_id: str, pinned: bool, user_id: str) -> bool:
        """置顶 / 取消置顶。"""
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE chat_threads
                    SET pinned = %s, updated_at = NOW()
                    WHERE thread_id = %s AND user_id = %s
                    """,
                    (pinned, thread_id, user_id),
                )
                return (cur.rowcount or 0) > 0

    def delete_thread(self, thread_id: str, user_id: str) -> bool:
        """删除会话元数据记录（LangGraph checkpoint 的清理由调用方决定）。"""
        pool = self._get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM chat_threads WHERE thread_id = %s AND user_id = %s",
                    (thread_id, user_id),
                )
                return (cur.rowcount or 0) > 0
