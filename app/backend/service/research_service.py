"""研究服务层：纯 async generator + graph.astream 实现 token 级流式。

P2 重写：替换 workflow_service.py 的 Thread+Queue 桥接为纯 async generator。
- stream_research(): 主流式入口，直接挂 StreamingResponse
- run(): 非流式入口（向后兼容 /run 端点）
- Thread CRUD 从 workflow_service.py 迁移

三不变式保证：
1. 流一定结束：completed/cancelled/error 在各自分支内发出
2. delta 顺序拼接完整：astream 顺序消费顺序 yield
3. 前端忽略未知 type：协议层约定
"""

import asyncio
import logging
import os
import time
import uuid
from collections.abc import AsyncGenerator
from threading import Lock
from typing import Optional

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from mult_agents.config import AppConfig
from mult_agents.graph import build_app as build_workflow_app
from mult_agents.runtime import build_checkpointer
from mult_agents.models import build_agents
from mult_agents.state import create_initial_state
from mult_agents.research_logger import get_research_logger, close_research_logger
from backend.schemas.events import event, sse, EventEnvelope
from backend.infra import ThreadRepository, generate_thread_title
from backend.service.memory_service import get_memory_service

# ── P6-6: 会话标题 LLM 自动生成 ───────────────────────────────────
import asyncio as _asyncio

_title_gen_lock = _asyncio.Lock()


async def _generate_llm_title(query: str, report_summary: str, api_key: str) -> str:
    """P6-6: 用 qwen-turbo 从用户问题+报告摘要生成 ≤20 字标题。"""
    from langchain_community.chat_models import ChatTongyi
    from langchain_core.messages import HumanMessage as _HM

    llm = ChatTongyi(model="qwen-turbo", temperature=0.1, dashscope_api_key=api_key)
    prompt = (
        f"请根据以下用户提问和研究报告摘要，生成一个不超过20个字的简洁中文标题。\n"
        f"只输出标题文字，不要引号、不要标点。\n\n"
        f"用户提问：{query[:200]}\n"
        f"报告摘要：{report_summary[:500]}"
    )
    try:
        resp = await llm.ainvoke([_HM(content=prompt)])
        title = resp.content.strip().strip('"\'').strip()
        if title and len(title) <= 30:
            return title
        return ""
    except Exception as exc:
        logger.warning("LLM 标题生成失败: %s", exc)
        return ""

logger = logging.getLogger("backend.research_service")

# 节点中文标签
NODE_LABELS = {
    "intent": "意图识别",
    "direct_answer": "快速回答",
    "clarify": "问题澄清",
    "plan": "研究规划",
    "web_search": "网络检索",
    "local_rag": "知识库检索",
    "deep_dive": "证据裁判",
    "analyze": "综合分析",
    "reflect": "补充搜索",
    "write": "报告撰写",
}


class ResearchService:
    """研究服务：管理图执行、流式输出与会话元数据。

    替代旧 WorkflowService 的核心流式逻辑，不含后台线程/队列。
    """

    def __init__(self, config_path: str):
        self._config_path = config_path
        self._lock = Lock()
        self._initialized = False
        self._base_config: AppConfig | None = None
        self._app = None
        self._thread_repo: Optional[ThreadRepository] = None

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            base_config = AppConfig.from_file(self._config_path)
            agents = build_agents(base_config.model, base_config.api_key, base_config)
            checkpointer = build_checkpointer(base_config)
            self._app = build_workflow_app(agents, checkpointer)
            self._base_config = base_config
            if base_config.postgres_dsn:
                try:
                    self._thread_repo = ThreadRepository(dsn=base_config.postgres_dsn)
                except Exception as exc:
                    logger.warning("会话元数据仓储初始化失败: %s", exc)
            self._initialized = True

    def _build_runtime_config(
        self,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None,
        enable_memory: bool | None,
        hitl_enabled: bool | None = None,
    ) -> AppConfig:
        if self._base_config is None:
            raise RuntimeError("service not initialized")
        overrides = {
            "user_id": user_id,
            "thread_id": thread_id,
            "tenant_id": tenant_id,
            "max_iterations": max_iterations if max_iterations is not None else self._base_config.max_iterations,
        }
        if enable_memory is not None:
            overrides["enable_memory"] = enable_memory
        if hitl_enabled is not None:
            overrides["hitl_enabled"] = hitl_enabled
        return self._base_config.with_overrides(**overrides)

    def _build_initial_state(
        self,
        query: str,
        runtime_config: AppConfig,
        memory_context: str = "",
    ) -> dict:
        """构建初始状态（memory_context 由调用方异步获取后传入）。"""
        return create_initial_state(
            query=query,
            max_iterations=runtime_config.max_iterations,
            user_id=runtime_config.user_id,
            tenant_id=runtime_config.tenant_id,
            memory_context=memory_context,
            hitl_enabled=runtime_config.hitl_enabled,
            hitl_config=runtime_config.hitl_config,
        )

    # ── 流式入口 ──────────────────────────────────────

    async def stream_research(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None = None,
        enable_memory: bool | None = None,
        hitl_enabled: bool | None = None,
    ) -> AsyncGenerator[str, None]:
        """纯 async generator，直接挂 StreamingResponse；无后台线程、无队列。

        yields SSE 格式的字符串：data: {json}\n\n
        """
        self._ensure_initialized()
        run_id = uuid.uuid4().hex[:12]
        t0 = time.time()

        logger.info("[TRACE] stream_research START | run=%s | thread=%s | user=%s | query=%s",
                     run_id, thread_id, user_id, query[:120])

        runtime_config = self._build_runtime_config(
            user_id, thread_id, tenant_id, max_iterations, enable_memory, hitl_enabled
        )

        # P5: 热路径检索 — 异步获取记忆注入 plan system prompt
        memory_context = ""
        if runtime_config.enable_memory:
            mem_service = get_memory_service()
            if mem_service is not None:
                try:
                    memory_context = await mem_service.hot_path_search(
                        runtime_config.user_id, query
                    )
                except Exception as exc:
                    logger.warning("热路径记忆检索失败: %s", exc)

        input_state = create_initial_state(
            query=query,
            max_iterations=runtime_config.max_iterations,
            user_id=runtime_config.user_id,
            tenant_id=runtime_config.tenant_id,
            memory_context=memory_context,
            hitl_enabled=runtime_config.hitl_enabled,
            hitl_config=runtime_config.hitl_config,
        )
        config = {"configurable": {"thread_id": runtime_config.thread_id}}

        # 落库会话记录
        self._record_thread(thread_id, user_id, title=generate_thread_title(query))

        # 研究日志
        research_logger = get_research_logger(thread_id)
        research_logger.log_event("task_start", {"query": query, "thread_id": thread_id})
        research_logger.update_content("query", query)

        final = ""
        route = "multiagent"
        seen_nodes: set[str] = set()

        # 1. 发送 run.started
        yield sse(event("run.started", thread_id=thread_id, run_id=run_id))

        try:
            async for mode, chunk in self._app.astream(
                input_state, config, stream_mode=["custom", "updates"]
            ):
                if mode == "custom":
                    # 节点内 StreamWriter 发出的自定义事件
                    if isinstance(chunk, dict):
                        evt_type = chunk.get("type", "")

                        # token 级流式：LLM 输出的增量 text
                        if evt_type == "token":
                            node = chunk.get("node", "")
                            text = chunk.get("text", "")
                            mid = f"{run_id}:{node}"
                            # 首次出现该节点时发 message.start
                            if node not in seen_nodes:
                                seen_nodes.add(node)
                                yield sse(event("message.start", message_id=mid, node=node))
                            yield sse(event("message.delta", message_id=mid, text=text))

                        # 进度消息
                        elif evt_type == "progress":
                            node = chunk.get("node", "")
                            message = chunk.get("message", "")
                            label = NODE_LABELS.get(node, node)
                            yield sse(event("agent.status", node=node, label=label, phase="running"))

                        # 旧格式兼容：{node: "...", message: "..."}
                        elif "node" in chunk and "message" in chunk and "type" not in chunk:
                            node = chunk.get("node", "")
                            message = chunk.get("message", "")
                            label = NODE_LABELS.get(node, node)
                            yield sse(event("agent.status", node=node, label=label, phase="running"))

                        # sources.found
                        elif evt_type == "sources":
                            sources = chunk.get("sources", [])
                            yield sse(event("sources.found", sources=sources))

                    continue

                if mode == "updates":
                    if not isinstance(chunk, dict):
                        continue

                    # interrupt 检测
                    if "__interrupt__" in chunk:
                        interrupts = chunk["__interrupt__"]
                        for intr in interrupts:
                            # P4: 从 interrupt payload 中提取实际 kind（不再硬编码）
                            intr_value = intr.value if isinstance(intr.value, dict) else {"value": intr.value}
                            intr_kind = intr_value.get("kind", "unknown") if isinstance(intr_value, dict) else "unknown"
                            yield sse(event("interrupt.raised",
                                            interrupt_id=intr.id,
                                            kind=intr_kind,
                                            payload=intr_value))
                        break

                    for node_name, node_output in chunk.items():
                        if node_name == "__interrupt__":
                            continue

                        label = NODE_LABELS.get(node_name, node_name)
                        yield sse(event("agent.status", node=node_name, label=label, phase="completed"))

                        if isinstance(node_output, dict):
                            research_logger.log_event("node_complete", {"node": node_name})

                            # 提取 intent
                            if node_name == "intent":
                                detected = str(node_output.get("intent", route)).strip().lower()
                                if detected in {"direct", "multiagent"}:
                                    route = detected
                                research_logger.update_content("intent", route)

                            # 提取 final
                            value = node_output.get("final")
                            if value:
                                final = str(value)

            # 2. 正常结束：发 run.completed + P5 后台记忆提取
            if final:
                self._complete_thread(thread_id, intent=route)
                close_research_logger(thread_id, route=route, final=final)
                logger.info("[TRACE] stream_research DONE | run=%s | thread=%s | route=%s | final_len=%d | elapsed=%.2fs",
                             run_id, thread_id, route, len(final), time.time() - t0)
                yield sse(event("run.completed", message_id=f"{run_id}:write", final_state="done"))
                # P5: 后台记忆提取（run.completed 后异步触发，不阻塞）
                self._trigger_memory_extract(runtime_config, query, final, thread_id)
                # P6-6: LLM 标题生成（run.completed 后异步，不阻塞）
                self._trigger_title_gen(runtime_config, query, final, thread_id)
            else:
                # 尝试从快照获取 final
                snapshot = self._app.get_state(config)
                final = str(snapshot.values.get("final", ""))
                if final:
                    self._complete_thread(thread_id, intent=route)
                    close_research_logger(thread_id, route=route, final=final)
                    yield sse(event("run.completed", message_id=f"{run_id}:write", final_state="done"))
                    # P5: 后台记忆提取
                    self._trigger_memory_extract(runtime_config, query, final, thread_id)
                    # P6-6: LLM 标题生成
                    self._trigger_title_gen(runtime_config, query, final, thread_id)
                else:
                    logger.warning("[TRACE] stream_research NO-FINAL | run=%s | thread=%s", run_id, thread_id)
                    yield sse(event("run.error", code="NoFinalOutput", message="研究链路未产生最终结果"))

        except asyncio.CancelledError:
            # 用户取消 —— 结束事件在此发出后重新抛出
            logger.info("[TRACE] stream_research CANCELLED | run=%s | thread=%s", run_id, thread_id)
            close_research_logger(thread_id, route=route, final=final)
            yield sse(event("run.cancelled", reason="user_cancelled"))
            raise

        except Exception as e:
            # 任何异常必发 run.error，随后自然关闭 generator
            logger.error("[TRACE] stream_research ERROR | run=%s | thread=%s | error=%s",
                         run_id, thread_id, e, exc_info=True)
            close_research_logger(thread_id, route=route, final=final)
            yield sse(event("run.error", code=type(e).__name__, message=str(e)))

        # ⚠️ 无 finally —— 结构性修复旧 workflow_service.py:641 的 NameError 挂起问题

    # ── 非流式入口（向后兼容 /run）──────────────────────

    async def run(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None = None,
        enable_memory: bool | None = None,
        hitl_enabled: bool | None = None,
    ) -> str:
        """非流式执行，收集全部 message.delta 拼接为最终文本。"""
        self._ensure_initialized()
        t0 = time.time()
        req_id = uuid.uuid4().hex[:8]
        logger.info("[TRACE] run START | req=%s | thread=%s | query=%s", req_id, thread_id, query[:120])

        runtime_config = self._build_runtime_config(
            user_id, thread_id, tenant_id, max_iterations, enable_memory, hitl_enabled
        )

        # P5: 热路径检索
        memory_context = ""
        if runtime_config.enable_memory:
            mem_service = get_memory_service()
            if mem_service is not None:
                try:
                    memory_context = await mem_service.hot_path_search(
                        runtime_config.user_id, query
                    )
                except Exception as exc:
                    logger.warning("热路径记忆检索失败: %s", exc)

        input_state = create_initial_state(
            query=query,
            max_iterations=runtime_config.max_iterations,
            user_id=runtime_config.user_id,
            tenant_id=runtime_config.tenant_id,
            memory_context=memory_context,
            hitl_enabled=runtime_config.hitl_enabled,
            hitl_config=runtime_config.hitl_config,
        )
        config = {"configurable": {"thread_id": runtime_config.thread_id}}

        try:
            result = self._app.invoke(input_state, config)
        except Exception as exc:
            logger.error("[TRACE] run ERROR | req=%s | thread=%s | error=%s", req_id, thread_id, exc, exc_info=True)
            raise

        final = str(result.get("final", ""))
        route = str(result.get("intent", "multiagent"))
        logger.info("[TRACE] run DONE | req=%s | thread=%s | route=%s | final_len=%d | elapsed=%.2fs",
                     req_id, thread_id, route, len(final), time.time() - t0)

        # P5: 后台记忆提取
        if runtime_config.enable_memory and final:
            self._trigger_memory_extract(runtime_config, query, final, thread_id)
        return final

    async def run_with_route(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None = None,
        enable_memory: bool | None = None,
        hitl_enabled: bool | None = None,
    ) -> tuple[str, str]:
        """非流式执行，返回 (final, route)。"""
        self._ensure_initialized()
        runtime_config = self._build_runtime_config(
            user_id, thread_id, tenant_id, max_iterations, enable_memory, hitl_enabled
        )

        # P5: 热路径检索
        memory_context = ""
        if runtime_config.enable_memory:
            mem_service = get_memory_service()
            if mem_service is not None:
                try:
                    memory_context = await mem_service.hot_path_search(
                        runtime_config.user_id, query
                    )
                except Exception as exc:
                    logger.warning("热路径记忆检索失败: %s", exc)

        input_state = create_initial_state(
            query=query,
            max_iterations=runtime_config.max_iterations,
            user_id=runtime_config.user_id,
            tenant_id=runtime_config.tenant_id,
            memory_context=memory_context,
            hitl_enabled=runtime_config.hitl_enabled,
            hitl_config=runtime_config.hitl_config,
        )
        config = {"configurable": {"thread_id": runtime_config.thread_id}}

        result = self._app.invoke(input_state, config)
        final = str(result.get("final", ""))
        route = str(result.get("intent", "multiagent")).strip().lower()

        # P5: 后台记忆提取
        if runtime_config.enable_memory and final:
            self._trigger_memory_extract(runtime_config, query, final, thread_id)
        return final, route

    # ── 会话元数据（从 workflow_service.py 迁移）─────────

    def _record_thread(self, thread_id: str, user_id: str, title: str = "", intent: str = "", completed: bool = False) -> None:
        if self._thread_repo is None:
            return
        try:
            self._thread_repo.upsert_thread(thread_id=thread_id, user_id=user_id, title=title, intent=intent, completed=completed)
        except Exception as exc:
            logger.warning("会话记录落库失败 | thread_id=%s | %s", thread_id, exc)

    def _complete_thread(self, thread_id: str, intent: str = "") -> None:
        if self._thread_repo is None:
            return
        try:
            self._thread_repo.mark_completed(thread_id, intent=intent)
        except Exception as exc:
            logger.warning("会话完成标记失败 | thread_id=%s | %s", thread_id, exc)

    def _trigger_memory_extract(
        self,
        runtime_config: AppConfig,
        query: str,
        final: str,
        thread_id: str,
    ) -> None:
        """P5: 触发后台记忆提取（run.completed 后，不阻塞主流程）。"""
        mem_service = get_memory_service()
        if mem_service is None:
            return
        try:
            messages = [HumanMessage(content=query)]
            from langchain_core.messages import AIMessage
            messages.append(AIMessage(content=final))
            mem_service.trigger_background_extract(
                user_id=runtime_config.user_id,
                thread_id=thread_id,
                messages=messages,
            )
        except Exception as exc:
            logger.warning("后台记忆提取触发失败: %s", exc)

    def _trigger_memory_extract_from_snapshot(
        self,
        thread_id: str,
        final: str,
        config: dict,
    ) -> None:
        """P5: 从快照中提取 query 后触发后台记忆提取（resume_stream 用）。"""
        mem_service = get_memory_service()
        if mem_service is None:
            return
        try:
            snapshot = self._app.get_state(config)
            query = str(snapshot.values.get("query", ""))
            user_id = str(snapshot.values.get("user_id", "default_user"))
            if not query:
                return
            messages = [HumanMessage(content=query)]
            from langchain_core.messages import AIMessage
            messages.append(AIMessage(content=final))
            mem_service.trigger_background_extract(
                user_id=user_id,
                thread_id=thread_id,
                messages=messages,
            )
        except Exception as exc:
            logger.warning("后台记忆提取触发失败(resume): %s", exc)

    def _trigger_title_gen(
        self,
        runtime_config: AppConfig,
        query: str,
        final: str,
        thread_id: str,
    ) -> None:
        """P6-6: run.completed 后异步用 LLM 生成标题（不阻塞主流程）。"""
        api_key = runtime_config.api_key
        if not api_key:
            return
        user_id = runtime_config.user_id
        repo = self._thread_repo

        async def _do_title():
            title = await _generate_llm_title(query, final[:500], api_key)
            if title and repo is not None:
                try:
                    repo.rename_thread(thread_id, title, user_id)
                    logger.info("[P6-6] LLM 标题生成完成 | thread=%s | title=%s", thread_id, title)
                except Exception as exc:
                    logger.warning("LLM 标题落库失败: %s", exc)

        try:
            loop = asyncio.get_event_loop()
            loop.create_task(_do_title())
        except Exception as exc:
            logger.warning("LLM 标题生成触发失败: %s", exc)

    def list_threads(self, user_id: str, limit: int = 50, keyword: str = "") -> list[dict]:
        self._ensure_initialized()
        if self._thread_repo is not None:
            try:
                return self._thread_repo.list_threads(user_id=user_id, limit=limit, keyword=keyword)
            except Exception as exc:
                logger.warning("读取会话列表失败: %s", exc)
        return []

    def rename_thread(self, thread_id: str, title: str, user_id: str) -> bool:
        self._ensure_initialized()
        if self._thread_repo is None:
            return False
        return self._thread_repo.rename_thread(thread_id, title, user_id)

    def set_thread_pinned(self, thread_id: str, pinned: bool, user_id: str) -> bool:
        self._ensure_initialized()
        if self._thread_repo is None:
            return False
        return self._thread_repo.set_pinned(thread_id, pinned, user_id)

    def delete_thread(self, thread_id: str, user_id: str) -> bool:
        self._ensure_initialized()
        if self._thread_repo is None:
            return False
        return self._thread_repo.delete_thread(thread_id, user_id)

    def get_state(self, thread_id: str) -> dict:
        """获取任务当前状态快照（P3 增强）。

        返回字段：
        - thread_id: 会话 ID
        - status: idle | running | awaiting_input | interrupted_by_restart
        - current_node: 当前执行到的节点名
        - has_checkpoint: 是否有 checkpoint（可恢复）
        - resumable: 是否可恢复
        - interrupted_by_restart: 是否因进程重启被中断
        - next_nodes: 下一步待执行的节点列表
        - values: 核心状态值子集
        - interrupts: 当前 interrupt 信息（如有）
        """
        self._ensure_initialized()
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = self._app.get_state(config)

        # 判断状态
        from backend.service import get_task_registry
        registry = get_task_registry()
        is_running = registry.is_running(thread_id)

        has_interrupts = bool(snapshot.interrupts)
        has_next = bool(snapshot.next)

        if is_running:
            status = "running"
        elif has_interrupts:
            status = "awaiting_input"
        elif has_next:
            # 有待执行节点但不在运行 → 可能是崩溃中断
            # 检查 Redis interrupted_by_restart 标记
            import asyncio as _asyncio
            try:
                loop = _asyncio.get_event_loop()
                if loop.is_running():
                    # 异步上下文，但这里是同步方法，用安全方式检查
                    pass
            except Exception:
                pass
            status = "idle"  # 默认 idle，router 层异步检查 interrupted_by_restart
        else:
            status = "idle"

        # next_nodes
        next_nodes = list(snapshot.next) if snapshot.next else []

        # current_node：从 next 推断
        current_node = next_nodes[0] if next_nodes else ""

        return {
            "thread_id": thread_id,
            "status": status,
            "current_node": current_node,
            "has_checkpoint": snapshot.parent_config is not None or has_next or bool(snapshot.values),
            "resumable": has_next or has_interrupts,
            "interrupted_by_restart": False,  # router 层异步补充
            "next_nodes": next_nodes,
            "values": {k: v for k, v in snapshot.values.items() if k in (
                "query", "phase", "intent", "iteration", "plan", "final",
                "hitl_enabled", "user_feedback", "needs_more_research",
            )},
            "interrupts": [
                {"id": intr.id, "value": intr.value}
                for intr in snapshot.interrupts
            ] if snapshot.interrupts else [],
            "created_at": snapshot.created_at,
            "parent_config": snapshot.parent_config,
        }

    # ── P4: interrupt 状态重建 API ──

    async def get_interrupt(self, thread_id: str) -> dict:
        """获取当前 interrupt 信息，供前端重建审批卡片。

        P4-3: 从 graph.get_state().tasks[*].interrupts 读取，
        返回结构化审批数据（含 kind/payload）。
        """
        self._ensure_initialized()
        config = {"configurable": {"thread_id": thread_id}}
        try:
            snapshot = await self._app.aget_state(config)
        except Exception:
            # aget_state 不可用时回退到同步
            snapshot = self._app.get_state(config)

        if not snapshot.next or not snapshot.tasks:
            return {"active": False, "thread_id": thread_id}

        for task in snapshot.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                intr = task.interrupts[0]
                value = intr.value if isinstance(intr.value, dict) else {"value": intr.value}
                kind = value.get("kind", "unknown")
                return {
                    "active": True,
                    "thread_id": thread_id,
                    "interrupt_id": intr.id,
                    "kind": kind,
                    "payload": value,
                }

        return {"active": False, "thread_id": thread_id}

    def get_state_history(self, thread_id: str, limit: int = 20) -> list[dict]:
        self._ensure_initialized()
        config = {"configurable": {"thread_id": thread_id}}
        history = []
        for i, snapshot in enumerate(self._app.get_state_history(config)):
            if i >= limit:
                break
            history.append({
                "checkpoint_id": snapshot.config.get("configurable", {}).get("checkpoint_id", ""),
                "next": list(snapshot.next) if snapshot.next else [],
                "created_at": snapshot.created_at,
                "interrupts_count": len(snapshot.interrupts) if snapshot.interrupts else 0,
            })
        return history

    def update_state(self, thread_id: str, values: dict, as_node: str | None = None) -> dict:
        self._ensure_initialized()
        config = {"configurable": {"thread_id": thread_id}}
        self._app.update_state(config, values, as_node=as_node)
        return {"thread_id": thread_id, "updated": True}

    def get_thread_messages(self, thread_id: str, limit: int = 100) -> list[dict]:
        self._ensure_initialized()
        messages = []
        try:
            config = {"configurable": {"thread_id": thread_id}}
            snapshot = self._app.get_state(config)
            if not snapshot or not snapshot.values:
                return []
            state_msgs = snapshot.values.get("messages", [])
            for msg in state_msgs[-limit:]:
                role = getattr(msg, "type", "unknown")
                content = getattr(msg, "content", str(msg))
                if role == "human":
                    messages.append({"role": "user", "content": content})
                elif role == "ai":
                    messages.append({"role": "assistant", "content": content})
            final = snapshot.values.get("final", "")
            if final and (not messages or messages[-1]["content"] != final):
                messages.append({"role": "assistant", "content": final})
            if not messages:
                query = snapshot.values.get("query", "")
                if query:
                    messages.append({"role": "user", "content": query})
                if final:
                    messages.append({"role": "assistant", "content": final})
        except Exception as exc:
            logger.warning("获取会话消息失败: %s", exc)
        return messages

    # ── 恢复（P3 重写）──────────────────────

    async def resume_stream(
        self,
        thread_id: str,
        resume_value: dict | str | None = None,
        mode: str = "answer",
    ) -> AsyncGenerator[str, None]:
        """流式恢复中断的任务（P3 重写）。

        两种模式：
        - mode=continue: 崩溃续研，用 astream(None, config) 从最后 checkpoint 续跑
          （None 输入 = 从断点节点开始，已检索的 sources/findings 全部保留）
        - mode=answer: HITL 回答，用 Command(resume=resume_value) 从 interrupt 点继续
          （P4 会扩展 resume_value 为结构化 payload）

        Args:
            thread_id: 会话 ID
            resume_value: HITL 回答值（mode=answer 时必填，mode=continue 时忽略）
            mode: "continue" | "answer"
        """
        self._ensure_initialized()
        config = {"configurable": {"thread_id": thread_id}}
        run_id = uuid.uuid4().hex[:12]
        final = ""

        logger.info("[TRACE] resume_stream START | run=%s | thread=%s | mode=%s", run_id, thread_id, mode)

        yield sse(event("run.started", thread_id=thread_id, run_id=run_id))

        # 输入路由：mode=continue → None（从最后 checkpoint 续跑）；mode=answer → Command(resume=...)
        if mode == "continue":
            input_state = None
            logger.info("[TRACE] resume_stream CONTINUE | thread=%s | 从最后 checkpoint 续跑", thread_id)
        else:
            if resume_value is None:
                yield sse(event("run.error", code="InvalidResume", message="mode=answer 需要 resume_value"))
                return
            input_state = Command(resume=resume_value)
            logger.info("[TRACE] resume_stream ANSWER | thread=%s | resume_value=%s",
                        thread_id, str(resume_value)[:100])

        try:
            async for mode_chunk, chunk in self._app.astream(
                input_state, config, stream_mode=["custom", "updates"]
            ):
                if mode_chunk == "custom":
                    if isinstance(chunk, dict):
                        evt_type = chunk.get("type", "")
                        if evt_type == "token":
                            node = chunk.get("node", "")
                            text = chunk.get("text", "")
                            mid = f"{run_id}:{node}"
                            yield sse(event("message.delta", message_id=mid, text=text))
                        elif evt_type == "progress":
                            node = chunk.get("node", "")
                            label = NODE_LABELS.get(node, node)
                            yield sse(event("agent.status", node=node, label=label, phase="running"))
                        elif evt_type == "sources":
                            sources = chunk.get("sources", [])
                            yield sse(event("sources.found", sources=sources))
                    continue

                if mode_chunk == "updates":
                    if not isinstance(chunk, dict):
                        continue

                    # interrupt 检测
                    if "__interrupt__" in chunk:
                        interrupts = chunk["__interrupt__"]
                        for intr in interrupts:
                            # P4: 从 interrupt payload 中提取实际 kind（不再硬编码）
                            intr_value = intr.value if isinstance(intr.value, dict) else {"value": intr.value}
                            intr_kind = intr_value.get("kind", "unknown") if isinstance(intr_value, dict) else "unknown"
                            yield sse(event("interrupt.raised",
                                            interrupt_id=intr.id,
                                            kind=intr_kind,
                                            payload=intr_value))
                        break

                    for node_name, node_output in chunk.items():
                        if node_name == "__interrupt__":
                            continue
                        label = NODE_LABELS.get(node_name, node_name)
                        yield sse(event("agent.status", node=node_name, label=label, phase="completed"))
                        if isinstance(node_output, dict):
                            value = node_output.get("final")
                            if value:
                                final = str(value)

            # 尝试获取 final
            if final:
                self._complete_thread(thread_id, intent="multiagent")
                close_research_logger(thread_id, route="multiagent", final=final)
                logger.info("[TRACE] resume_stream DONE | run=%s | thread=%s | final_len=%d",
                             run_id, thread_id, len(final))
                yield sse(event("run.completed", message_id=f"{run_id}:write", final_state="done"))
                # P5: 后台记忆提取
                self._trigger_memory_extract_from_snapshot(thread_id, final, config)
            else:
                snapshot = self._app.get_state(config)
                final = str(snapshot.values.get("final", ""))
                if final:
                    self._complete_thread(thread_id, intent="multiagent")
                    close_research_logger(thread_id, route="multiagent", final=final)
                    yield sse(event("run.completed", message_id=f"{run_id}:write", final_state="done"))
                    # P5: 后台记忆提取
                    self._trigger_memory_extract_from_snapshot(thread_id, final, config)
                else:
                    logger.warning("[TRACE] resume_stream NO-FINAL | run=%s | thread=%s", run_id, thread_id)
                    yield sse(event("run.error", code="NoFinalOutput", message="恢复完成但未获得最终结果"))

        except asyncio.CancelledError:
            logger.info("[TRACE] resume_stream CANCELLED | run=%s | thread=%s", run_id, thread_id)
            close_research_logger(thread_id, route="multiagent", final=final)
            yield sse(event("run.cancelled", reason="user_cancelled"))
            raise

        except Exception as e:
            logger.error("[TRACE] resume_stream ERROR | run=%s | thread=%s | error=%s",
                         run_id, thread_id, e, exc_info=True)
            close_research_logger(thread_id, route="multiagent", final=final)
            yield sse(event("run.error", code=type(e).__name__, message=str(e)))

        # ⚠️ 无 finally —— 与 stream_research 一致的结构性保证


# ── 单例 ──────────────────────────────────────────

_SERVICE: ResearchService | None = None


def get_research_service() -> ResearchService:
    global _SERVICE
    if _SERVICE is None:
        import os
        config_path = os.getenv("CONFIG_PATH", "app/config.json")
        _SERVICE = ResearchService(config_path=config_path)
    return _SERVICE
