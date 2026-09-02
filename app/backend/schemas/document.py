"""文档上传相关 Pydantic 模型。"""

from pydantic import BaseModel


class DocumentUploadResponse(BaseModel):
    """文件上传成功后的响应（主链路）。"""

    filename: str
    doc_id: str
    object_key: str
    chunks: int
    status: str          # queued (已入队) | failed
    message: str


class DocumentInfo(BaseModel):
    """文档信息摘要（含向量化进度，供列表页直接渲染）。"""

    doc_id: str
    object_key: str
    filename: str
    file_ext: str
    file_size: int
    content_hash: str
    user_id: str
    status: str
    chunk_count: int
    created_at: str
    # ── 向量化进度 ──
    indexed_chunks: int = 0
    pending_chunks: int = 0
    failed_chunks: int = 0
    progress: int = 0            # 0-100
    vector_state: str = "empty"  # empty | processing | indexed | partial | failed


class DocumentListResponse(BaseModel):
    """文档列表响应。"""

    documents: list[DocumentInfo]
    total: int
    stats: "DocumentStatsResponse | None" = None


class DocumentStatsResponse(BaseModel):
    """知识库总览统计。"""

    document_count: int = 0
    ready_documents: int = 0     # 全部切片已进向量库、可被检索的文档数
    failed_documents: int = 0
    chunk_count: int = 0
    indexed_chunks: int = 0
    pending_chunks: int = 0
    failed_chunks: int = 0
    size_bytes: int = 0
    progress: int = 0            # 全库切片索引进度 0-100


class DocumentDeleteResponse(BaseModel):
    """文档删除响应。"""

    deleted: bool
    doc_id: str
    message: str


class DocumentBatchDeleteRequest(BaseModel):
    """批量删除请求。"""

    doc_ids: list[str]
    user_id: str = "default_user"


class DocumentBatchDeleteResponse(BaseModel):
    """批量删除响应。"""

    deleted: int
    doc_ids: list[str]
    message: str


class DocumentRetryResponse(BaseModel):
    """重试向量化响应。"""

    doc_id: str
    retried: int
    published: int
    message: str


class DocumentStatusResponse(BaseModel):
    """文档状态查询响应。"""

    doc_id: str
    filename: str
    status: str
    chunk_count: int
    vector_status: dict   # {"pending": N, "indexed": M, "failed": K}
