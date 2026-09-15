"""
文档上传与入库服务 — 主链路 + 异步链路分离架构。

改造前（同步全链路）:
  上传 → 本地保存 → 解析 → 向量化 → Milvus 入库
  问题: Embedding + Milvus 写入耗时阻塞 API，不可扩展

改造后（主链路 + 异步链路）:

【主链路（同步，毫秒级返回）】
  1. 上传文档 → MinIO（返回 ObjectKey）
  2. 解析切块（复用 RAG 系统的语义切分逻辑）
  3. PG 本地事务: 写入 documents + document_chunks + chunk_sync_messages
  4. 发送消息到 MQ（Topic: chunk-sync）
  5. 返回 doc_id, chunks 数

【异步链路（MQ 消费者，后台线程）】
  1. 消费者拉取 chunk-sync.queue 消息
  2. 调用 Embedding 模型生成向量
  3. 写入 Milvus（子块向量 + 父块向量）
  4. 更新 PG chunk vector_status = 'indexed'
  5. 手动 ACK 消息

参考:
  - RAGFlow (89.6k star): 文件上传到 MinIO → DB 记录 → 异步任务执行解析+embedding
  - Dify (90k+ star): 文档上传 → 对象存储 → DB 记录 → Celery 异步索引
  - 本地消息表 (Outbox Pattern): 保证 PG 事务和 MQ 发送的最终一致性
"""

import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter, MarkdownHeaderTextSplitter

from mult_agents.config import AppConfig
from mult_agents.rag.core import (
    RAGConfig,
    RAGSystem,
    PDFParser,
    create_parent_splitter,
    create_semantic_chunker,
    split_parent_child,
)
import mult_agents.tools as _tools_mod
from mult_agents.tools import init_rag_system

from backend.infra import (
    MinIOStorage,
    ChunkRepository,
    DocumentRecord,
    ChunkRecord,
    MQProducer,
    ensure_tables,
)

logger = logging.getLogger("backend.document_service")

# ── 支持的文件格式 ──────────────────────────────────────────────────
LOADER_MAP: dict[str, tuple[str, str]] = {
    ".pdf":  ("langchain_community.document_loaders", "PyPDFLoader"),
    ".docx": ("langchain_community.document_loaders", "Docx2txtLoader"),
    ".doc":  ("langchain_community.document_loaders", "UnstructuredWordDocumentLoader"),
    ".md":   ("langchain_community.document_loaders", "TextLoader"),
    ".markdown": ("langchain_community.document_loaders", "TextLoader"),
    ".txt":  ("langchain_community.document_loaders", "TextLoader"),
    ".html": ("langchain_community.document_loaders", "UnstructuredHTMLLoader"),
    ".htm":  ("langchain_community.document_loaders", "UnstructuredHTMLLoader"),
    ".csv":  ("langchain_community.document_loaders", "CSVLoader"),
    ".json": ("langchain_community.document_loaders", "JSONLoader"),
}

ALLOWED_EXTENSIONS = sorted(LOADER_MAP.keys())


def _import_loader(file_ext: str):
    """根据文件扩展名动态导入对应的 LangChain Document Loader。"""
    if file_ext not in LOADER_MAP:
        raise ValueError(
            f"不支持的文件格式: {file_ext}，支持: {', '.join(ALLOWED_EXTENSIONS)}"
        )
    module_path, class_name = LOADER_MAP[file_ext]
    import importlib
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _clean_text(text: str) -> str:
    """基础文本清洗：去除多余空白、控制字符。"""
    import re
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


class DocumentService:
    """
    文档上传服务 — 主链路实现。

    主链路（同步）:
      上传 → MinIO → 解析切块 → PG 本地事务 → MQ 发送
    """

    def __init__(self):
        self._config: Optional[AppConfig] = None
        self._rag: Optional[RAGSystem] = None
        self._minio: Optional[MinIOStorage] = None
        self._repo: Optional[ChunkRepository] = None
        self._mq: Optional[MQProducer] = None
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self._config = AppConfig.from_file()

        # 初始化 PG 表结构
        ensure_tables(self._config.postgres_dsn)

        # 初始化 MinIO 存储
        minio_endpoint = getattr(self._config, 'minio_endpoint', 'localhost:9900')
        minio_access_key = getattr(self._config, 'minio_access_key', 'minioadmin')
        minio_secret_key = getattr(self._config, 'minio_secret_key', 'minioadmin')
        minio_bucket = getattr(self._config, 'minio_bucket', 'deep-research-docs')
        minio_secure = getattr(self._config, 'minio_secure', False)
        self._minio = MinIOStorage(
            endpoint=minio_endpoint,
            access_key=minio_access_key,
            secret_key=minio_secret_key,
            bucket=minio_bucket,
            secure=minio_secure,
        )

        # 初始化 PG 仓库
        self._repo = ChunkRepository(dsn=self._config.postgres_dsn)

        # 初始化 MQ 生产者
        rabbitmq_url = getattr(self._config, 'rabbitmq_url', '')
        rabbitmq_exchange = getattr(self._config, 'rabbitmq_chunk_sync_exchange', 'chunk-sync')
        self._mq: Optional[MQProducer] = MQProducer(url=rabbitmq_url, exchange=rabbitmq_exchange)
        try:
            self._mq.connect()
        except Exception as exc:
            # 必须置 None：否则后续 `if self._mq is not None` 会走「已连接」分支，
            # 同步向量化降级永远不会触发，切片会静默停在 pending。
            logger.warning("MQ 连接失败，转为同步向量化降级: %s", exc)
            self._mq = None

        # 初始化 RAG 系统（复用全局实例，用于解析切块）
        # collection 名走 RAGConfig 默认常量，与 app_main / runtime.build_agents 保持一致
        rag_config = RAGConfig(
            milvus_host=self._config.milvus_host,
            milvus_port=self._config.milvus_port,
            postgres_dsn=self._config.postgres_dsn,
        )
        if _tools_mod._RAG_SYSTEM is None:
            try:
                init_rag_system(api_key=self._config.api_key, config=rag_config)
            except RuntimeError as exc:
                logger.error("DocumentService: RAG 系统初始化失败: %s", exc)
        self._rag = _tools_mod._RAG_SYSTEM
        if self._rag is None:
            logger.warning("DocumentService: RAG 系统不可用，文档入库功能将受限")

        self._initialized = True
        logger.info("DocumentService 初始化完成（主链路 + 异步链路架构）")

    def upload_and_ingest(
        self,
        file_content: bytes,
        filename: str,
        user_id: str = "default_user",
    ) -> dict:
        """
        主链路：上传文档 → MinIO → 解析切块 → PG 本地事务 → MQ 发送。

        返回 dict:
          - filename: 原始文件名
          - doc_id: 文档唯一标识（PG documents.id）
          - object_key: MinIO 对象存储 key
          - chunks: 切块数量
          - status: "queued" (已入队等待向量化) | "failed"
          - message: 描述信息
        """
        self._ensure_initialized()
        assert self._minio is not None
        assert self._repo is not None

        file_ext = Path(filename).suffix.lower()
        if file_ext not in LOADER_MAP:
            raise ValueError(
                f"不支持的文件格式: {file_ext}，支持: {', '.join(ALLOWED_EXTENSIONS)}"
            )

        content_hash = hashlib.md5(file_content).hexdigest()

        # ── Step 1: 上传到 MinIO ─────────────────────────────────
        content_type_map = {
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ".doc": "application/msword",
            ".md": "text/markdown",
            ".txt": "text/plain",
            ".html": "text/html",
            ".htm": "text/html",
            ".csv": "text/csv",
            ".json": "application/json",
        }
        content_type = content_type_map.get(file_ext, "application/octet-stream")
        object_key = self._minio.upload_file(
            file_content=file_content,
            filename=filename,
            content_type=content_type,
        )

        # ── Step 2: 解析文档 ───────────────────────────────────
        try:
            chunks = self._parse_and_chunk(
                file_content=file_content,
                filename=filename,
                file_ext=file_ext,
            )
        except Exception as exc:
            logger.error("文档解析失败: %s | file=%s", exc, filename)
            return {
                "filename": filename,
                "doc_id": "",
                "object_key": object_key,
                "chunks": 0,
                "status": "failed",
                "message": f"文档解析失败: {exc}",
            }

        if not chunks:
            return {
                "filename": filename,
                "doc_id": "",
                "object_key": object_key,
                "chunks": 0,
                "status": "failed",
                "message": "文档内容为空或解析无结果",
            }

        # ── Step 3: PG 本地事务 + MQ 发送 ──────────────────────
        doc_id = uuid.uuid4().hex
        source_name = Path(filename).name

        doc_record = DocumentRecord(
            id=doc_id,
            object_key=object_key,
            filename=filename,
            file_ext=file_ext,
            file_size=len(file_content),
            content_hash=content_hash,
            user_id=user_id,
            status="parsed",
            chunk_count=len(chunks),
        )

        # 构建 chunk 记录
        chunk_records = []
        for idx, (chunk_content, metadata) in enumerate(chunks):
            chunk_id = uuid.uuid4().hex
            chunk_hash = hashlib.md5(chunk_content.encode()).hexdigest()
            parent_id = metadata.get("parent_id", "")
            section_path = metadata.get("section_path", "")
            chunk_records.append(ChunkRecord(
                id=chunk_id,
                doc_id=doc_id,
                parent_id=parent_id,
                chunk_idx=idx,
                content=chunk_content,
                content_hash=chunk_hash,
                section_path=section_path,
                metadata={
                    **metadata,
                    "source_name": source_name,
                    "doc_id": doc_id,
                },
                vector_status="pending",
            ))

        # PG 本地事务: documents + document_chunks + chunk_sync_messages 原子写入
        message_ids = self._repo.insert_document_with_chunks(
            doc=doc_record,
            chunks=chunk_records,
        )

        # MQ 发送（发送失败由补偿任务在消费者启动时补发）
        mq_messages = []
        for mid, chunk in zip(message_ids, chunk_records):
            mq_messages.append({
                "id": mid,
                "doc_id": chunk.doc_id,
                "chunk_id": chunk.id,
                "payload": json.dumps({
                    "doc_id": chunk.doc_id,
                    "chunk_id": chunk.id,
                    "parent_id": chunk.parent_id,
                    "chunk_idx": chunk.chunk_idx,
                    "content": chunk.content,
                    "content_hash": chunk.content_hash,
                    "section_path": chunk.section_path,
                    "metadata": chunk.metadata,
                    "source_name": source_name,
                }, ensure_ascii=False),
            })

        # publish_chunks 内部把异常吞成 return False，因此必须检查返回值：
        # 无条件 mark_messages_sent 会让消息在 Outbox 里变成 sent，
        # 补偿任务再也不会重发，切片就永久停在 pending（静默失败）。
        mq_ok = self._publish_messages(mq_messages, message_ids)

        # 降级方案: MQ 不可用（未连接或发送失败）且 RAG 可用时，同步向量化
        if not mq_ok and self._rag is not None:
            logger.info("MQ 不可用，启动同步向量化降级模式")
            try:
                self._sync_vectorize_chunks(chunk_records, source_name)
                return {
                    "filename": filename,
                    "doc_id": doc_id,
                    "object_key": object_key,
                    "chunks": len(chunks),
                    "status": "indexed",
                    "message": f"文档已上传并切分为 {len(chunks)} 个片段，已同步向量化完成",
                }
            except Exception as exc:
                logger.error("同步向量化降级失败: %s", exc)
                return {
                    "filename": filename,
                    "doc_id": doc_id,
                    "object_key": object_key,
                    "chunks": len(chunks),
                    "status": "queued",
                    "message": f"文档已上传切分为 {len(chunks)} 个片段，向量化失败: {exc}",
                }

        logger.info(
            "主链路完成 | doc_id=%s | filename=%s | object_key=%s | chunks=%d",
            doc_id, filename, object_key, len(chunks),
        )

        return {
            "filename": filename,
            "doc_id": doc_id,
            "object_key": object_key,
            "chunks": len(chunks),
            "status": "queued",
            "message": f"文档已上传并切分为 {len(chunks)} 个片段，已入队等待向量化",
        }

    def _parse_and_chunk(
        self,
        file_content: bytes,
        filename: str,
        file_ext: str,
    ) -> list[tuple[str, dict]]:
        """
        解析文档并执行语义切分，返回 (chunk_content, metadata) 列表。

        复用 RAG 系统的切分逻辑，但不执行向量化入库。
        """
        import tempfile

        # 写入临时文件用于 Loader 解析
        with tempfile.NamedTemporaryFile(
            suffix=file_ext, delete=False
        ) as tmp:
            tmp.write(file_content)
            tmp_path = Path(tmp.name)

        try:
            if file_ext == ".pdf":
                return self._parse_pdf(tmp_path, filename)
            else:
                return self._parse_general(tmp_path, file_ext, filename)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _parse_pdf(self, file_path: Path, filename: str) -> list[tuple[str, dict]]:
        """PDF 专用解析 + 切分。"""
        pdf_parser = PDFParser(enable_ocr_fallback=False)
        documents = pdf_parser.parse(file_path)
        if not documents:
            return []

        # 合并各页文本
        page_texts = []
        for doc in documents:
            page_num = doc.metadata.get("page", 0)
            page_texts.append(f"\n<!-- page_break: {page_num} -->\n{doc.page_content}")
        full_text = "\n\n".join(page_texts)
        full_text = _clean_text(full_text)

        return self._semantic_split(full_text, str(file_path), filename)

    def _parse_general(
        self,
        file_path: Path,
        file_ext: str,
        filename: str,
    ) -> list[tuple[str, dict]]:
        """通用文档解析 + 切分。"""
        loader_cls = _import_loader(file_ext)
        if file_ext in (".txt", ".md", ".markdown"):
            loader = loader_cls(str(file_path), encoding="utf-8")
        else:
            loader = loader_cls(str(file_path))

        documents = loader.load()
        if not documents:
            return []

        full_text = "\n\n".join(doc.page_content for doc in documents)
        full_text = _clean_text(full_text)
        if not full_text.strip():
            return []

        return self._semantic_split(full_text, str(file_path), filename)

    def _get_splitters(self):
        """取切分器：优先复用 RAG 系统的实例，RAG 未初始化时降级为本地构造。"""
        if self._rag is not None:
            return self._rag.markdown_splitter, self._rag.parent_splitter, self._rag.child_splitter

        logger.warning("RAG 系统未初始化，使用本地降级切分器")
        markdown_splitter, child_splitter = create_semantic_chunker()
        return markdown_splitter, create_parent_splitter(), child_splitter

    def _semantic_split(
        self,
        text: str,
        source: str,
        source_name: str,
    ) -> list[tuple[str, dict]]:
        """切分文本，返回 (子块内容, metadata) 列表。

        复用 rag.core 的 split_parent_child —— 父子分块只保留一份实现。
        历史上这里另抄了一份，把父块内容写成了子块内容，
        导致父子上下文扩展退化为返回子块副本。
        """
        markdown_splitter, parent_splitter, child_splitter = self._get_splitters()
        _, child_docs = split_parent_child(
            text,
            source,
            markdown_splitter=markdown_splitter,
            parent_splitter=parent_splitter,
            child_splitter=child_splitter,
        )
        for doc in child_docs:
            # source 是临时文件路径，这里用真实文件名覆盖
            doc.metadata["source_name"] = source_name
        return [(doc.page_content, doc.metadata) for doc in child_docs]

    def _sync_vectorize_chunks(
        self,
        chunk_records: list[ChunkRecord],
        source_name: str,
    ) -> None:
        """同步向量化降级方案: MQ 不可用时直接调用 RAG 系统写入 Milvus。"""
        if self._rag is None:
            raise RuntimeError("RAG 系统不可用，无法向量化")
        from langchain_core.documents import Document as LCDocument
        for chunk in chunk_records:
            try:
                child_doc = LCDocument(
                    page_content=chunk.content,
                    metadata={
                        "source": chunk.doc_id,
                        "source_name": source_name,
                        "section_path": chunk.section_path,
                        "parent_id": chunk.parent_id,
                        "child_id": chunk.metadata.get("child_id", ""),
                        "chunk_type": "child",
                        "chunk_idx": chunk.chunk_idx,
                        "doc_id": chunk.doc_id,
                    },
                )
                self._rag.vectorstore.add_documents([child_doc])
                self._rag.bm25.add_documents([child_doc])
                if chunk.parent_id and chunk.parent_id not in self._rag._parent_map:
                    parent_doc = LCDocument(
                        page_content=chunk.metadata.get("parent_content") or chunk.content,
                        metadata={
                            "source": chunk.doc_id,
                            "source_name": source_name,
                            "section_path": chunk.section_path,
                            "parent_id": chunk.parent_id,
                            "chunk_type": "parent",
                            "doc_id": chunk.doc_id,
                        },
                    )
                    self._rag.parent_store.add_documents([parent_doc])
                    self._rag._parent_map[chunk.parent_id] = parent_doc
                self._repo.update_chunk_vector_status(
                    chunk_id=chunk.id,
                    status="indexed",
                    milvus_pk="",
                )
            except Exception as exc:
                logger.error("同步向量化 chunk 失败 | chunk_id=%s | error=%s", chunk.id, exc)
                self._repo.update_chunk_vector_status(
                    chunk_id=chunk.id,
                    status="failed",
                    milvus_pk="",
                )

    def list_documents(self, user_id: str = "default_user", keyword: str = "") -> list[dict]:
        """
        列出已上传的文档（含向量化进度）。

        返回字段在原有基础上增加 indexed_chunks / pending_chunks /
        failed_chunks / progress / vector_state，前端可直接渲染
        「是否已进入向量库」，无需再逐文档轮询 status 接口。
        """
        self._ensure_initialized()
        assert self._repo is not None
        return self._repo.list_documents(user_id=user_id, keyword=keyword)

    def get_documents_stats(self, user_id: str = "default_user") -> dict:
        """知识库总览统计（文档数 / 切片数 / 已索引 / 待处理 / 失败）。"""
        self._ensure_initialized()
        assert self._repo is not None
        return self._repo.get_documents_stats(user_id=user_id)

    def _publish_messages(self, mq_messages: list[dict], message_ids: list[str]) -> bool:
        """投递消息到 MQ，成功则把 Outbox 标记为 sent。

        返回 True 表示消息确实投递成功、由 MQ 接管；
        返回 False 表示需要调用方走同步向量化降级。

        必须检查 publish_chunks 的返回值：它内部把异常吞成 return False 而不抛出，
        若调用方无条件 mark_messages_sent，消息会在 Outbox 里变成 sent，
        补偿任务再也不会重发，切片就永久停在 pending。
        """
        if self._mq is None:
            logger.warning("MQ 未连接，消息保留在 PG 本地消息表")
            return False

        try:
            published = bool(self._mq.publish_chunks(mq_messages))
        except Exception as exc:
            logger.warning("MQ 发送异常（消息保留 pending，由补偿任务补发）: %s", exc)
            return False

        if not published:
            logger.warning("MQ 发送失败，%d 条消息保留 pending 状态待补偿补发", len(message_ids))
            return False

        self._repo.mark_messages_sent(message_ids)
        return True

    def retry_failed_chunks(self, doc_id: str) -> dict:
        """
        重试未完成的切片（failed / pending）。

        把切片重置为 pending 并重建 Outbox 消息，然后立即投递 MQ
        （不依赖只在启动时执行一次的补偿扫描）。
        """
        self._ensure_initialized()
        assert self._repo is not None

        rows = self._repo.retry_failed_chunks(doc_id)
        if not rows:
            return {"retried": 0, "doc_id": doc_id, "message": "没有未完成的切片需要重试"}

        messages = [{"id": mid, "payload": payload} for mid, payload in rows]
        message_ids = [mid for mid, _ in rows]
        published = len(messages) if self._publish_messages(messages, message_ids) else 0

        return {
            "retried": len(rows),
            "published": published,
            "doc_id": doc_id,
            "message": f"已重置 {len(payloads)} 个失败切片并重新入队",
        }

    def delete_documents_batch(self, doc_ids: list[str], user_id: str) -> dict:
        """批量删除文档（PG + MinIO + 向量库）。"""
        self._ensure_initialized()
        assert self._repo is not None
        assert self._minio is not None

        deleted = self._repo.delete_documents_batch(doc_ids, user_id=user_id)
        if not deleted:
            return {
                "deleted": 0,
                "doc_ids": doc_ids,
                "message": "没有可删除的文档",
            }

        for _doc_id, object_key in deleted:
            try:
                self._minio.delete_file(object_key)
            except Exception as exc:  # pragma: no cover - 外部存储
                logger.warning("MinIO 删除失败 | key=%s | %s", object_key, exc)

        vector_errors = []
        for doc_id, _object_key in deleted:
            result = self._delete_vectors(doc_id)
            vector_errors.extend(result.get("errors", []))

        return {
            "deleted": len(deleted),
            "doc_ids": doc_ids,
            "message": f"已删除 {len(deleted)} 个文档（PG + MinIO + 向量库）",
            "vector_errors": vector_errors,
        }

    def delete_document(self, doc_id: str, user_id: str) -> dict:
        """删除文档（PG + MinIO + 向量库），仅限 user_id 名下。

        PG 的 document_chunks 由外键级联删除，但 Milvus 与 BM25 中的切片不会随之消失，
        必须显式清理，否则已删文档仍会被检索命中。
        """
        self._ensure_initialized()
        assert self._repo is not None
        assert self._minio is not None

        object_key = self._repo.delete_document(doc_id, user_id=user_id)
        if object_key is None:
            return {
                "deleted": False,
                "doc_id": doc_id,
                "message": "文档不存在",
            }

        self._minio.delete_file(object_key)
        vector_result = self._delete_vectors(doc_id)
        message = "文档已删除（PG + MinIO + 向量库）"
        if vector_result.get("errors"):
            message += "，但向量清理存在失败项，请检查日志"
        return {
            "deleted": True,
            "doc_id": doc_id,
            "message": message,
            "vectors": vector_result,
        }

    def _delete_vectors(self, doc_id: str) -> dict:
        """清理该文档在 Milvus 与 BM25 中的切片；RAG 不可用时返回 skipped。"""
        if self._rag is None:
            logger.warning("RAG 系统不可用，跳过向量清理 | doc_id=%s", doc_id)
            return {"skipped": "RAG 系统不可用"}
        try:
            return self._rag.delete_document_vectors(doc_id)
        except Exception as exc:  # pragma: no cover - 外部存储
            logger.warning("向量清理失败 | doc_id=%s | %s", doc_id, exc)
            return {"errors": [str(exc)]}

    def get_document_status(self, doc_id: str) -> dict:
        """查询文档向量化状态。"""
        self._ensure_initialized()
        assert self._repo is not None
        return self._repo.get_document_status(doc_id)


# ── 单例 ─────────────────────────────────────────────────────────
from functools import lru_cache


@lru_cache(maxsize=1)
def get_document_service() -> DocumentService:
    return DocumentService()
