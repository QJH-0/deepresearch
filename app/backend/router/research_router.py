import asyncio
import collections.abc
import json
import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse, Response
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
    ClarifyResumePayload,
    PlanApprovalResumePayload,
    ReportReviewResumePayload,
)
from backend.service import ResearchService, get_research_service
from backend.service import get_task_registry, ConcurrentRunError
from backend.service.task_registry import RunningTask

logger = logging.getLogger("backend.router.research")

router = APIRouter(prefix="/api/v1/research", tags=["research"])


class CancelRequest(BaseModel):
    thread_id: str


# ── P3: generator 包装器 — 注册到 TaskRegistry ──────────────

async def _stream_with_registry(
    thread_id: str,
    run_id: str,
    gen: collections.abc.AsyncGenerator[str, None],
) -> collections.abc.AsyncGenerator[str, None]:
    """包装 async generator：注册到 TaskRegistry，结束后自动清理。

    P3: 使 stream_research / resume_stream 的消费过程被注册到 TaskRegistry，
    从而支持 task.cancel() 在 LLM await 点真正中断 LLM 调用。
    """
    registry = get_task_registry()
    current_task = asyncio.current_task()

    if current_task is not None:
        existing = registry._tasks.get(thread_id)
        if existing is not None and not existing.task.done():
            # 并发运行 → 409
            raise ConcurrentRunError(thread_id)

        registry._tasks[thread_id] = RunningTask(
            thread_id=thread_id,
            run_id=run_id,
            task=current_task,
            started_at=time.time(),
        )
        current_task.add_done_callback(lambda _: registry._cleanup(thread_id))
        logger.info("[STREAM] TaskRegistry 注册 | thread=%s | run=%s", thread_id, run_id)

    try:
        async for chunk in gen:
            yield chunk
    except asyncio.CancelledError:
        logger.info("[STREAM] generator 被取消 | thread=%s | run=%s", thread_id, run_id)
        raise


# ── 路由 ──────────────────────────────────────────────

@router.post("/run", response_model=ResearchResponse)
async def run_research(
    payload: ResearchRequest,
    research_service: ResearchService = Depends(get_research_service),
) -> ResearchResponse:
    logger.info("[ROUTE] /run | user=%s | thread=%s | query=%s", payload.user_id, payload.thread_id, payload.query[:80])
    final = await research_service.run(
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
    research_service: ResearchService = Depends(get_research_service),
) -> StreamingResponse:
    """P2/P3: 纯 async generator + graph.astream 实现 token 级流式 SSE。

    P3: 通过 TaskRegistry 实现并发拦截（409）和取消（task.cancel()）。
    """
    logger.info("[ROUTE] /stream | user=%s | thread=%s | query=%s",
                payload.user_id, payload.thread_id, payload.query[:80])

    # P3: 并发检查 — 同一 thread 已有运行中的任务 → 409
    registry = get_task_registry()
    if registry.is_running(payload.thread_id):
        raise HTTPException(
            status_code=409,
            detail=f"Thread {payload.thread_id} already has a running task",
        )

    run_id = uuid.uuid4().hex[:12]
    raw_gen = research_service.stream_research(
        query=payload.query,
        user_id=payload.user_id,
        thread_id=payload.thread_id,
        tenant_id=payload.tenant_id,
        max_iterations=payload.max_iterations,
        enable_memory=payload.enable_memory,
        hitl_enabled=payload.hitl_enabled,
    )
    # 包装 generator：注册到 TaskRegistry
    wrapped_gen = _stream_with_registry(payload.thread_id, run_id, raw_gen)
    return StreamingResponse(
        wrapped_gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/cancel")
async def cancel_research(
    payload: CancelRequest,
):
    """取消正在运行的研究任务（P3：走 TaskRegistry）。

    返回:
        200 {"cancelled": true}  — 本进程命中并已发送 cancel()
        202 {"cancelled": false, "reason": "signal_sent"} — 仅 Redis 兜底信号
        200 {"cancelled": false, "reason": "not_running"} — 无运行中任务（幂等）
    """
    registry = get_task_registry()
    if not registry.is_running(payload.thread_id):
        # 幂等：未运行不报错
        return {"thread_id": payload.thread_id, "cancelled": False, "reason": "not_running"}

    hit = await registry.cancel(payload.thread_id)
    if hit:
        return {"thread_id": payload.thread_id, "cancelled": True}
    else:
        return JSONResponse(
            status_code=202,
            content={"thread_id": payload.thread_id, "cancelled": False, "reason": "signal_sent"},
        )


@router.post("/resume")
async def resume_research(
    payload: ResumeRequest,
    research_service: ResearchService = Depends(get_research_service),
) -> StreamingResponse:
    """恢复被中断的任务（流式输出）。

    P3: 支持 mode=continue（崩溃续研）和 mode=answer（HITL 回答）。
    P4: mode=answer 时按 interrupt kind 校验 resume_value 结构（不匹配 → 422）。
    """
    logger.info("[ROUTE] /resume | thread=%s | mode=%s | resume_value=%s",
                payload.thread_id, payload.mode,
                str(payload.resume_value)[:100] if payload.resume_value else "(empty)")

    # P3: 并发检查
    registry = get_task_registry()
    if registry.is_running(payload.thread_id):
        raise HTTPException(
            status_code=409,
            detail=f"Thread {payload.thread_id} already has a running task",
        )

    # P4-2: mode=answer 时按 interrupt kind 校验 payload
    if payload.mode == "answer" and payload.resume_value is not None:
        # 从 graph state 获取当前 interrupt 的 kind
        interrupt_info = await research_service.get_interrupt(payload.thread_id)
        if interrupt_info.get("active"):
            actual_kind = interrupt_info.get("kind", "unknown")
            resume_dict = payload.resume_value if isinstance(payload.resume_value, dict) else {}
            payload_kind = resume_dict.get("kind", "")
            # kind 不匹配 → 422
            if payload_kind and payload_kind != actual_kind:
                raise HTTPException(
                    status_code=422,
                    detail=f"Resume payload kind '{payload_kind}' does not match interrupt kind '{actual_kind}'",
                )
            # 按实际 kind 校验 payload 结构
            try:
                _validate_resume_payload(actual_kind, payload.resume_value)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))

    run_id = uuid.uuid4().hex[:12]
    raw_gen = research_service.resume_stream(
        thread_id=payload.thread_id,
        resume_value=payload.resume_value,
        mode=payload.mode,
    )
    # 包装 generator：注册到 TaskRegistry
    wrapped_gen = _stream_with_registry(payload.thread_id, run_id, raw_gen)
    return StreamingResponse(
        wrapped_gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _validate_resume_payload(kind: str, resume_value) -> None:
    """P4-2: 按 interrupt kind 校验 resume payload 结构，不合法则 raise ValueError。"""
    if not isinstance(resume_value, dict):
        raise ValueError("resume_value 必须是 dict")
    try:
        match kind:
            case "clarification":
                ClarifyResumePayload(**resume_value)
            case "plan_approval":
                PlanApprovalResumePayload(**resume_value)
            case "report_review":
                ReportReviewResumePayload(**resume_value)
            case _:
                raise ValueError(f"未知的 interrupt kind: {kind}")
    except Exception as exc:
        raise ValueError(f"Payload 校验失败: {exc}") from exc


@router.get("/threads", response_model=ThreadListResponse)
async def list_threads(
    user_id: str = "default_user",
    limit: int = 50,
    keyword: str = "",
    research_service: ResearchService = Depends(get_research_service),
) -> ThreadListResponse:
    """
    列出用户的所有会话历史。

    返回顺序：置顶优先，其余按最近活跃时间倒序（新会话在最上面）。
    keyword 用于按标题搜索，会话数超过 ~20 条后这是刚需。
    """
    threads = research_service.list_threads(user_id, limit, keyword=keyword)
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
    research_service: ResearchService = Depends(get_research_service),
):
    """重命名会话（自动生成的标题往往不够描述性，允许手动改）。"""
    research_service.rename_thread(
        thread_id=thread_id,
        title=payload.title,
        user_id=payload.user_id or "default_user",
    )
    threads = research_service.list_threads(payload.user_id or "default_user", 200)
    matched = next((t for t in threads if t["thread_id"] == thread_id), None)
    return ThreadItem(**matched) if matched else None


@router.post("/threads/{thread_id}/pin", response_model=ThreadItem | None)
async def pin_thread(
    thread_id: str,
    payload: ThreadPinRequest,
    research_service: ResearchService = Depends(get_research_service),
):
    """置顶 / 取消置顶会话（置顶项固定在列表顶部）。"""
    research_service.set_thread_pinned(
        thread_id=thread_id,
        pinned=payload.pinned,
        user_id=payload.user_id or "default_user",
    )
    threads = research_service.list_threads(payload.user_id or "default_user", 200)
    matched = next((t for t in threads if t["thread_id"] == thread_id), None)
    return ThreadItem(**matched) if matched else None


@router.delete("/threads/{thread_id}", response_model=ThreadDeleteResponse)
async def delete_thread(
    thread_id: str,
    user_id: str = "default_user",
    research_service: ResearchService = Depends(get_research_service),
) -> ThreadDeleteResponse:
    """删除会话（只删侧边栏记录，LangGraph checkpoint 保留以免影响可恢复状态）。"""
    deleted = research_service.delete_thread(thread_id, user_id)
    return ThreadDeleteResponse(
        deleted=deleted,
        thread_id=thread_id,
        message="会话已删除" if deleted else "会话不存在或无权删除",
    )


@router.get("/threads/{thread_id}/messages")
async def get_thread_messages(
    thread_id: str,
    research_service: ResearchService = Depends(get_research_service),
):
    """获取某个会话的完整对话历史。"""
    return {"thread_id": thread_id, "messages": research_service.get_thread_messages(thread_id)}


@router.get("/state/{thread_id}")
async def get_state(
    thread_id: str,
    research_service: ResearchService = Depends(get_research_service),
):
    """获取任务当前状态快照（P3 增强）。"""
    state = research_service.get_state(thread_id)

    # 异步补充 interrupted_by_restart 标记
    registry = get_task_registry()
    if registry.redis is not None:
        try:
            is_interrupted = await registry.is_interrupted_by_restart(thread_id)
            state["interrupted_by_restart"] = is_interrupted
            # 如果有中断标记且有待执行节点，状态修正为 interrupted_by_restart
            if is_interrupted and state["status"] == "idle" and state["next_nodes"]:
                state["status"] = "interrupted_by_restart"
        except Exception as exc:
            logger.warning("检查 interrupted_by_restart 失败: %s", exc)

    return state


@router.get("/threads/{thread_id}/state")
async def get_thread_state(
    thread_id: str,
    research_service: ResearchService = Depends(get_research_service),
):
    """P3: 会话级状态 API，返回完整的可恢复信息。

    与 /state/{thread_id} 功能相同，路径符合 RESTful 约定。
    """
    return await get_state(thread_id, research_service)


@router.get("/threads/{thread_id}/interrupt")
async def get_interrupt(
    thread_id: str,
    research_service: ResearchService = Depends(get_research_service),
):
    """P4-3: interrupt 状态重建 API。

    前端切会话时调用本接口重建审批卡片，不依赖内存。
    持久化由 PG checkpointer 天然保证（interrupt 状态存在 checkpoint 里）。

    返回:
        {active: true, interrupt_id, kind, payload}  — 有待处理的 interrupt
        {active: false, thread_id}                    — 无待处理 interrupt
    """
    return await research_service.get_interrupt(thread_id)


@router.get("/history/{thread_id}")
async def get_history(
    thread_id: str,
    limit: int = 20,
    research_service: ResearchService = Depends(get_research_service),
):
    """获取任务历史快照列表。"""
    return {"thread_id": thread_id, "history": research_service.get_state_history(thread_id, limit)}


@router.post("/rollback")
async def rollback(
    payload: RollbackRequest,
    research_service: ResearchService = Depends(get_research_service),
):
    """回滚/更新任务状态到指定值。"""
    return research_service.update_state(
        payload.thread_id, payload.values, as_node=payload.as_node
    )


# ── P5: 记忆 API ────────────────────────────────────

@router.get("/memories")
async def list_memories(
    user_id: str = "default_user",
    query: str = "",
    limit: int = 200,
):
    """P5: 列出用户全部记忆条目（只读优先）。

    Args:
        user_id: 用户 ID（预留多用户）
        query: 可选的语义查询（为空时返回全部）
        limit: 返回上限

    Returns:
        {memories: [{id, text, kind, created_at, updated_at}]}
    """
    from backend.service import get_memory_service

    mem_service = get_memory_service()
    if mem_service is None:
        return {"memories": [], "message": "MemoryService 未初始化"}

    memories = await mem_service.list_memories(
        user_id=user_id,
        query=query,
        limit=min(limit, 500),
    )
    return {"memories": memories, "total": len(memories)}


# ── P7-4: 导出 API ────────────────────────────────────

@router.get("/threads/{thread_id}/export/md")
async def export_markdown(
    thread_id: str,
    research_service: ResearchService = Depends(get_research_service),
):
    """P7-4: 导出会话最终报告为 Markdown 文件。

    返回 Content-Type: text/markdown，带 Content-Disposition 下载头。
    """
    messages = research_service.get_thread_messages(thread_id)
    if not messages:
        raise HTTPException(status_code=404, detail="会话无消息记录")

    # 拼接对话为 Markdown
    lines: list[str] = []
    for msg in messages:
        role_label = "用户" if msg.get("role") == "user" else "助手"
        lines.append(f"### {role_label}\n")
        lines.append(msg.get("content", ""))
        lines.append("")

    content = "\n".join(lines)
    filename = f"report_{thread_id[:12]}.md"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/threads/{thread_id}/export/pdf")
async def export_pdf(
    thread_id: str,
    research_service: ResearchService = Depends(get_research_service),
):
    """P7-4: 导出会话最终报告为 PDF。

    降级策略：
    1. 尝试 weasyprint 渲染
    2. 装不上 → 返回 HTML 打印页面（前端 window.print()）
    """
    messages = research_service.get_thread_messages(thread_id)
    if not messages:
        raise HTTPException(status_code=404, detail="会话无消息记录")

    # 取最后一条 assistant 消息作为报告正文
    report_content = ""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            report_content = msg.get("content", "")
            break

    if not report_content:
        raise HTTPException(status_code=404, detail="无可导出的报告内容")

    # 尝试 weasyprint
    try:
        from weasyprint import HTML

        html_content = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: 'Noto Sans CJK SC', 'Microsoft YaHei', sans-serif; line-height: 1.7; max-width: 700px; margin: 40px auto; color: #333; }}
h1, h2, h3 {{ color: #2c3e50; }}
sup.citation-ref {{ color: #3f67d4; font-size: 0.75em; }}
pre {{ background: #f5f5f5; padding: 12px; border-radius: 6px; overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ddd; padding: 6px 12px; }}
</style></head><body>
{report_content.replace(chr(10), '<br>')}
</body></html>"""
        pdf_bytes = HTML(string=html_content).write_pdf()
        filename = f"report_{thread_id[:12]}.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except ImportError:
        # 降级：返回 HTML 打印页面
        html_content = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>研究报告导出</title>
<style>
body {{ font-family: 'Microsoft YaHei', sans-serif; line-height: 1.7; max-width: 700px; margin: 40px auto; color: #333; }}
h1, h2, h3 {{ color: #2c3e50; }}
pre {{ background: #f5f5f5; padding: 12px; border-radius: 6px; overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ddd; padding: 6px 12px; }}
@media print {{ .no-print {{ display: none; }} }}
</style></head><body>
<div class="no-print" style="text-align:center; margin-bottom: 20px;">
<button onclick="window.print()" style="padding:8px 20px; font-size:14px; cursor:pointer;">🖨 打印为 PDF</button>
</div>
<pre style="white-space: pre-wrap; word-wrap: break-word;">{report_content}</pre>
</body></html>"""
        return Response(
            content=html_content.encode("utf-8"),
            media_type="text/html; charset=utf-8",
        )
    except Exception as exc:
        logger.error("PDF 导出失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"PDF 导出失败: {exc}")
