"""文档上传 REST API 路由。"""

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from backend.schemas import (
    DocumentBatchDeleteRequest,
    DocumentBatchDeleteResponse,
    DocumentDeleteResponse,
    DocumentListResponse,
    DocumentInfo,
    DocumentRetryResponse,
    DocumentStatsResponse,
    DocumentStatusResponse,
    DocumentUploadResponse,
)
from backend.service.document_service import DocumentService, get_document_service

logger = logging.getLogger("backend.document_router")

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

# 支持的文件扩展名
ALLOWED_EXTENSIONS = [
    ".pdf", ".docx", ".doc",
    ".md", ".markdown", ".txt",
    ".html", ".htm",
    ".csv", ".json",
]

# ── 上传限制（行业惯例：在上传前明示，而不是上传后才报错） ──
# Embedding 是按切片计费的，无上限的大文件会静默产生意外账单。
MAX_FILE_SIZE_MB = 50
MAX_FILES_PER_BATCH = 20
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024


@router.post("/upload", response_model=DocumentUploadResponse)
async def upload_document(
    file: UploadFile = File(..., description="要上传的文档文件"),
    user_id: str = Form(default="default_user"),
    service: DocumentService = Depends(get_document_service),
) -> DocumentUploadResponse:
    """
    上传文档文件 — 主链路（同步返回，向量化异步执行）。

    流程: 上传→MinIO→解析切块→PG本地事务→MQ发送→返回 doc_id

    支持格式: pdf, docx, doc, md, txt, html, csv, json
    """
    filename = file.filename or "unknown"
    file_ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if file_ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式: {file_ext}，支持: {', '.join(ALLOWED_EXTENSIONS)}",
        )

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="文件内容为空")
    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"文件过大: {len(content) / 1024 / 1024:.1f} MB，"
                f"单个文件上限 {MAX_FILE_SIZE_MB} MB"
            ),
        )
    result = service.upload_and_ingest(
        file_content=content,
        filename=filename,
        user_id=user_id,
    )
    return DocumentUploadResponse(**result)


@router.get("/list", response_model=DocumentListResponse)
async def list_documents(
    user_id: str = "default_user",
    keyword: str = "",
    with_stats: bool = True,
    service: DocumentService = Depends(get_document_service),
) -> DocumentListResponse:
    """
    列出已上传的文档（含每个文档的向量化进度）。

    keyword 用于文件名模糊搜索；with_stats 附带知识库总览统计，
    省掉前端再发一次统计请求。
    """
    docs = service.list_documents(user_id=user_id, keyword=keyword)
    stats = service.get_documents_stats(user_id=user_id) if with_stats else None
    return DocumentListResponse(
        documents=[DocumentInfo(**doc) for doc in docs],
        total=len(docs),
        stats=DocumentStatsResponse(**stats) if stats else None,
    )


@router.get("/stats", response_model=DocumentStatsResponse)
async def get_documents_stats(
    user_id: str = "default_user",
    service: DocumentService = Depends(get_document_service),
) -> DocumentStatsResponse:
    """知识库总览统计：文档数 / 切片数 / 已进向量库数量 / 失败数。"""
    return DocumentStatsResponse(**service.get_documents_stats(user_id=user_id))


@router.delete("/batch", response_model=DocumentBatchDeleteResponse)
async def batch_delete_documents(
    payload: DocumentBatchDeleteRequest,
    service: DocumentService = Depends(get_document_service),
) -> DocumentBatchDeleteResponse:
    """批量删除文档（PG + MinIO）。"""
    result = service.delete_documents_batch(
        doc_ids=payload.doc_ids,
        user_id=payload.user_id or "default_user",
    )
    return DocumentBatchDeleteResponse(**result)


@router.delete("/{doc_id}", response_model=DocumentDeleteResponse)
async def delete_document(
    doc_id: str,
    service: DocumentService = Depends(get_document_service),
) -> DocumentDeleteResponse:
    """删除已上传的文档。"""
    result = service.delete_document(doc_id)
    return DocumentDeleteResponse(**result)


@router.get("/status/{doc_id}", response_model=DocumentStatusResponse)
async def get_document_status(
    doc_id: str,
    service: DocumentService = Depends(get_document_service),
) -> DocumentStatusResponse:
    """查询文档向量化状态。"""
    result = service.get_document_status(doc_id)
    if not result:
        return DocumentStatusResponse(
            doc_id=doc_id,
            filename="",
            status="not_found",
            chunk_count=0,
            vector_status={},
        )
    return DocumentStatusResponse(**result)


@router.post("/{doc_id}/retry", response_model=DocumentRetryResponse)
async def retry_document_vectorization(
    doc_id: str,
    service: DocumentService = Depends(get_document_service),
) -> DocumentRetryResponse:
    """重试该文档向量化失败的切片（重置为 pending 并重新入队 MQ）。"""
    result = service.retry_failed_chunks(doc_id)
    return DocumentRetryResponse(**result)


@router.get("/extensions")
async def get_supported_extensions() -> dict:
    """返回支持的文件格式列表。"""
    return {
        "extensions": ALLOWED_EXTENSIONS,
        "max_file_size_mb": MAX_FILE_SIZE_MB,
        "max_files_per_batch": MAX_FILES_PER_BATCH,
    }
