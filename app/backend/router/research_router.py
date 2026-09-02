import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from backend.schemas import (
    ResearchRequest,
    ResearchResponse,
    ResumeRequest,
    RollbackRequest,
    ThreadItem,
    ThreadListResponse,
    ThreadRenameRequest,
    ThreadPinRequest,
    ThreadDeleteResponse,
)
from backend.service import WorkflowService, get_workflow_service

logger = logging.getLogger("backend.router.research")

router = APIRouter(prefix="/api/v1/research", tags=["research"])


class CancelRequest(BaseModel):
    thread_id: str


@router.post("/run", response_model=ResearchResponse)
async def run_research(
    payload: ResearchRequest,
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> ResearchResponse:
    logger.info("[ROUTE] /run | user=%s | thread=%s | query=%s", payload.user_id, payload.thread_id, payload.query[:80])
    final = await workflow_service.run(
        query=payload.query,
        user_id=payload.user_id,
        thread_id=payload.thread_id,
        tenant_id=payload.tenant_id,
        max_iterations=payload.max_iterations,
        enable_memory=payload.enable_memory,
        hitl_enabled=payload.hitl_enabled,
    )
    return ResearchResponse(
        query=payload.query,
        user_id=payload.user_id,
        thread_id=payload.thread_id,
        tenant_id=payload.tenant_id,
        final=final,
    )


@router.post("/stream")
async def stream_research(
    payload: ResearchRequest,
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> StreamingResponse:
    logger.info("[ROUTE] /stream | user=%s | thread=%s | query=%s", payload.user_id, payload.thread_id, payload.query[:80])
    async def event_stream():
        start_event = {"type": "status", "message": "任务已接收，正在初始化多智能体链路"}
        yield f"data: {json.dumps(start_event, ensure_ascii=False)}\n\n"
        async for event in workflow_service.stream_events(
            query=payload.query,
            user_id=payload.user_id,
            thread_id=payload.thread_id,
            tenant_id=payload.tenant_id,
            max_iterations=payload.max_iterations,
            enable_memory=payload.enable_memory,
            hitl_enabled=payload.hitl_enabled,
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/cancel")
async def cancel_research(
    payload: CancelRequest,
    workflow_service: WorkflowService = Depends(get_workflow_service),
):
    """取消正在运行的研究任务。"""
    success = workflow_service.cancel_task(payload.thread_id)
    return {"thread_id": payload.thread_id, "cancelled": success}


@router.post("/resume")
async def resume_research(
    payload: ResumeRequest,
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> StreamingResponse:
    """恢复被中断的任务（流式输出）。"""
    logger.info("[ROUTE] /resume | thread=%s | resume_value=%s", payload.thread_id, str(payload.resume_value)[:100] if payload.resume_value else "(empty)")
    async def event_stream():
        async for event in workflow_service.resume_stream(
            thread_id=payload.thread_id,
            resume_value=payload.resume_value,
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/threads", response_model=ThreadListResponse)
async def list_threads(
    user_id: str = "default_user",
    limit: int = 50,
    keyword: str = "",
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> ThreadListResponse:
    """
    列出用户的所有会话历史。

    返回顺序：置顶优先，其余按最近活跃时间倒序（新会话在最上面）。
    keyword 用于按标题搜索，会话数超过 ~20 条后这是刚需。
    """
    threads = workflow_service.list_threads(user_id, limit, keyword=keyword)
    return ThreadListResponse(
        threads=[ThreadItem(**t) for t in threads],
        total=len(threads),
    )


# ── 注意：下面这几个具体路径必须写在 /threads/{thread_id} 之前，
#    否则 FastAPI 会把 "rename" 当成 thread_id 匹配掉。──
@router.patch("/threads/{thread_id}/rename", response_model=ThreadItem | None)
async def rename_thread(
    thread_id: str,
    payload: ThreadRenameRequest,
    workflow_service: WorkflowService = Depends(get_workflow_service),
):
    """重命名会话（自动生成的标题往往不够描述性，允许手动改）。"""
    workflow_service.rename_thread(
        thread_id=thread_id,
        title=payload.title,
        user_id=payload.user_id or "default_user",
    )
    threads = workflow_service.list_threads(payload.user_id or "default_user", 200)
    matched = next((t for t in threads if t["thread_id"] == thread_id), None)
    return ThreadItem(**matched) if matched else None


@router.post("/threads/{thread_id}/pin", response_model=ThreadItem | None)
async def pin_thread(
    thread_id: str,
    payload: ThreadPinRequest,
    workflow_service: WorkflowService = Depends(get_workflow_service),
):
    """置顶 / 取消置顶会话（置顶项固定在列表顶部）。"""
    workflow_service.set_thread_pinned(
        thread_id=thread_id,
        pinned=payload.pinned,
        user_id=payload.user_id or "default_user",
    )
    threads = workflow_service.list_threads(payload.user_id or "default_user", 200)
    matched = next((t for t in threads if t["thread_id"] == thread_id), None)
    return ThreadItem(**matched) if matched else None


@router.delete("/threads/{thread_id}", response_model=ThreadDeleteResponse)
async def delete_thread(
    thread_id: str,
    user_id: str = "default_user",
    workflow_service: WorkflowService = Depends(get_workflow_service),
) -> ThreadDeleteResponse:
    """删除会话（只删侧边栏记录，LangGraph checkpoint 保留以免影响可恢复状态）。"""
    deleted = workflow_service.delete_thread(thread_id, user_id)
    return ThreadDeleteResponse(
        deleted=deleted,
        thread_id=thread_id,
        message="会话已删除" if deleted else "会话不存在或无权删除",
    )


@router.get("/threads/{thread_id}/messages")
async def get_thread_messages(
    thread_id: str,
    workflow_service: WorkflowService = Depends(get_workflow_service),
):
    """获取某个会话的完整对话历史。"""
    return {"thread_id": thread_id, "messages": workflow_service.get_thread_messages(thread_id)}


@router.get("/state/{thread_id}")
async def get_state(
    thread_id: str,
    workflow_service: WorkflowService = Depends(get_workflow_service),
):
    """获取任务当前状态快照。"""
    return workflow_service.get_state(thread_id)


@router.get("/history/{thread_id}")
async def get_history(
    thread_id: str,
    limit: int = 20,
    workflow_service: WorkflowService = Depends(get_workflow_service),
):
    """获取任务历史快照列表。"""
    return {"thread_id": thread_id, "history": workflow_service.get_state_history(thread_id, limit)}


@router.post("/rollback")
async def rollback(
    payload: RollbackRequest,
    workflow_service: WorkflowService = Depends(get_workflow_service),
):
    """回滚/更新任务状态到指定值。"""
    return workflow_service.update_state(
        payload.thread_id, payload.values, as_node=payload.as_node
    )
