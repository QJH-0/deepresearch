import asyncio
import logging
import time
import uuid
from threading import Lock, Thread, Event
from typing import AsyncIterator, Callable, Optional

from langgraph.types import Command

from mult_agents.config import AppConfig
from mult_agents.graph import build_app as build_workflow_app
from mult_agents.runtime import build_agents, build_checkpointer, build_memory_manager
from mult_agents.state import create_initial_state
from mult_agents.research_logger import get_research_logger, close_research_logger
from backend.infra import ThreadRepository, generate_thread_title

logger = logging.getLogger("backend.workflow_service")


def _request_id() -> str:
    return uuid.uuid4().hex[:8]


class WorkflowService:
    def __init__(self, config_path: str):
        self._config_path = config_path
        self._lock = Lock()
        self._initialized = False
        self._base_config: AppConfig | None = None
        self._memory_manager = None
        self._app = None
        self._thread_repo: Optional[ThreadRepository] = None
        # ── 中断控制：允许前端取消正在运行的任务 ──
        self._cancel_flags: dict[str, Event] = {}

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._lock:
            if self._initialized:
                return
            base_config = AppConfig.from_file(self._config_path)
            self._memory_manager = build_memory_manager(base_config)
            agents = build_agents(base_config.model, base_config.api_key, base_config)
            checkpointer = build_checkpointer(base_config)
            self._app = build_workflow_app(agents, checkpointer)
            self._base_config = base_config
            # 会话元数据仓储（独立于 LangGraph checkpointer）
            if base_config.postgres_dsn:
                try:
                    self._thread_repo = ThreadRepository(dsn=base_config.postgres_dsn)
                except Exception as exc:  # pragma: no cover - PG 不可用时降级
                    logger.warning("会话元数据仓储初始化失败（会话历史将不可用）: %s", exc)
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

    def _run_sync(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None,
        enable_memory: bool | None,
        hitl_enabled: bool | None = None,
    ) -> tuple[str, str]:
        self._ensure_initialized()
        t0 = time.time()
        req_id = _request_id()
        logger.info("[TRACE] _run_sync START | req=%s | thread=%s | user=%s | hitl=%s | query=%s",
                     req_id, thread_id, user_id, hitl_enabled, query[:120])
        runtime_config = self._build_runtime_config(
            user_id=user_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            max_iterations=max_iterations,
            enable_memory=enable_memory,
            hitl_enabled=hitl_enabled,
        )
        memory_context = ""
        if self._memory_manager and runtime_config.enable_memory:
            memory_context = self._memory_manager.build_personalized_prompt_context(
                user_id=runtime_config.user_id,
                thread_id=runtime_config.thread_id,
                query=query,
                tenant_id=runtime_config.tenant_id,
                max_memories=runtime_config.memory_top_k,
            )
        state = create_initial_state(
            query=query,
            max_iterations=runtime_config.max_iterations,
            user_id=runtime_config.user_id,
            tenant_id=runtime_config.tenant_id,
            memory_context=memory_context,
            hitl_enabled=runtime_config.hitl_enabled,
            hitl_config=runtime_config.hitl_config,
        )
        config = {"configurable": {"thread_id": runtime_config.thread_id}}
        logger.info("[TRACE] _run_sync INVOKE | req=%s | thread=%s | max_iter=%s",
                     req_id, runtime_config.thread_id, runtime_config.max_iterations)
        try:
            result = self._app.invoke(state, config)
        except Exception as exc:
            logger.error("[TRACE] _run_sync INVOKE-EXCEPTION | req=%s | thread=%s | error=%s",
                          req_id, runtime_config.thread_id, exc, exc_info=True)
            raise
        final = result.get("final", "")
        route = str(result.get("intent", "multiagent"))
        logger.info("[TRACE] _run_sync DONE | req=%s | thread=%s | route=%s | final_len=%d | elapsed=%.2fs",
                     req_id, runtime_config.thread_id, route, len(final), time.time() - t0)
        if self._memory_manager and runtime_config.enable_memory and final:
            self._memory_manager.persist_turn(
                tenant_id=runtime_config.tenant_id,
                user_id=runtime_config.user_id,
                thread_id=runtime_config.thread_id,
                query=query,
                answer=final,
            )
        return final, route

    @staticmethod
    def _node_message(node_name: str) -> str:
        mapping = {
            "intent": "Intent Router 正在识别问题意图",
            "direct_answer": "Direct Responder 正在快速作答",
            "plan": "Planner 正在拆解问题",
            "web_search": "Web Scout 正在检索网络证据",
            "local_rag": "Local Scout 正在检索本地知识库",
            "deep_dive": "Evidence Judge 正在进行证据裁判",
            "analyze": "Analyst 正在生成结论",
            "reflect": "Reflect 正在生成补搜计划",
            "write": "Writer 正在撰写最终报告",
        }
        return mapping.get(node_name, f"{node_name} 正在执行")

    @staticmethod
    def _node_detail_message(node_name: str, node_output: dict) -> str:
        """从节点输出中提取有意义的中间状态详情，给用户更丰富的进度信息。"""
        try:
            if node_name == "intent":
                route = node_output.get("intent", "")
                return f"意图识别完成：{route}"
            if node_name == "plan":
                plan = str(node_output.get("plan", ""))[:120]
                subs = node_output.get("sub_questions", [])
                return f"计划已生成：{plan}...\n子问题 {len(subs)} 个"
            if node_name == "web_search":
                evidence = node_output.get("web_evidence", [])
                stats = node_output.get("web_retrieval_stats", {})
                return f"网络检索完成：召回 {len(evidence)} 条证据，查询 {stats.get('query_count', 0)} 次"
            if node_name == "local_rag":
                evidence = node_output.get("local_evidence", [])
                return f"本地知识库检索完成：召回 {len(evidence)} 条证据"
            if node_name == "deep_dive":
                pool = node_output.get("evidence_pool", [])
                return f"证据裁判完成：筛选后保留 {len(pool)} 条高质量证据"
            if node_name == "analyze":
                analysis = str(node_output.get("analysis_summary", ""))[:120]
                needs_more = node_output.get("needs_more_research", False)
                suffix = "，需要补充搜索" if needs_more else "，证据充分"
                return f"分析完成：{analysis}...{suffix}"
            if node_name == "reflect":
                queries = node_output.get("supplementary_queries", [])
                return f"补搜计划已生成：{len(queries)} 个补搜查询"
            if node_name == "write":
                draft = str(node_output.get("draft", ""))[:80]
                return f"报告撰写完成：{draft}..."
            if node_name == "direct_answer":
                return "快速回答已生成"
        except Exception:
            pass
        return ""

    def _run_sync_with_events(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None,
        enable_memory: bool | None,
        emit: Callable[[dict], None],
        hitl_enabled: bool | None = None,
        cancel_event: Optional[Event] = None,
    ) -> tuple[str, str]:
        self._ensure_initialized()
        runtime_config = self._build_runtime_config(
            user_id=user_id,
            thread_id=thread_id,
            tenant_id=tenant_id,
            max_iterations=max_iterations,
            enable_memory=enable_memory,
            hitl_enabled=hitl_enabled,
        )
        memory_context = ""
        if self._memory_manager and runtime_config.enable_memory:
            memory_context = self._memory_manager.build_personalized_prompt_context(
                user_id=runtime_config.user_id,
                thread_id=runtime_config.thread_id,
                query=query,
                tenant_id=runtime_config.tenant_id,
                max_memories=runtime_config.memory_top_k,
            )
        state = create_initial_state(
            query=query,
            max_iterations=runtime_config.max_iterations,
            user_id=runtime_config.user_id,
            tenant_id=runtime_config.tenant_id,
            memory_context=memory_context,
            hitl_enabled=runtime_config.hitl_enabled,
            hitl_config=runtime_config.hitl_config,
        )
        final = ""
        route = "multiagent"
        config = {"configurable": {"thread_id": runtime_config.thread_id}}
        # 参考LangGraph官方stream_mode="custom"机制（open-canvas项目）：
        # 同时监听 "updates"（节点完成事件）和 "custom"（节点内实时推送）
        # custom 事件由节点内部的 StreamWriter 实时推送，实现逐 token 级别的流式输出
        # 初始化结构化日志记录器（参考 gpt-researcher 的 JSONResearchHandler）
        research_logger = get_research_logger(runtime_config.thread_id)
        research_logger.log_event("task_start", {"query": query, "thread_id": runtime_config.thread_id})
        research_logger.update_content("query", query)
        for stream_chunk in self._app.stream(state, config, stream_mode=["updates", "custom"]):
            # 多 stream_mode 模式下，每个 chunk 是 (mode, data) 元组
            # 参考 LangGraph 官方文档 stream_mode 参数说明
            if not isinstance(stream_chunk, tuple) or len(stream_chunk) != 2:
                continue
            mode, update = stream_chunk

            # "custom" 模式：节点内部 StreamWriter 推送的实时事件
            # 参考 open-canvas 的 streamWorker 处理方式
            if mode == "custom":
                if isinstance(update, dict):
                    # 实时推送自定义事件到前端（如 token 级流式输出）
                    emit({"type": "custom", **update})
                continue

            # "updates" 模式：节点完成事件
            if not isinstance(update, dict):
                continue
            # 检测 interrupt
            if "__interrupt__" in update:
                interrupts = update["__interrupt__"]
                for intr in interrupts:
                    emit({
                        "type": "interrupt",
                        "interrupt_id": intr.id,
                        "node": intr.value.get("node", "") if isinstance(intr.value, dict) else "",
                        "value": intr.value,
                        "thread_id": runtime_config.thread_id,
                        "resumable": True,
                    })
                break  # 中断后退出，等待用户恢复
            for node_name, node_output in update.items():
                # 检查是否被用户取消
                if cancel_event and cancel_event.is_set():
                    emit({"type": "status", "message": "任务已被用户中断"})
                    research_logger.log_event("task_cancelled", {"node": node_name})
                    return "", route
                if node_name == "__interrupt__":
                    continue
                emit({"type": "phase", "node": node_name, "message": self._node_message(str(node_name))})
                # 参考 gpt-researcher 的 JSONResearchHandler：记录节点事件到结构化日志
                research_logger.log_event("node_complete", {"node": node_name, "has_output": isinstance(node_output, dict)})
                if isinstance(node_output, dict):
                    detail = self._node_detail_message(str(node_name), node_output)
                    if detail:
                        emit({"type": "status", "message": detail, "node": node_name})
                        research_logger.log_event("node_detail", {"node": node_name, "detail": detail})
                    if node_name == "intent":
                        detected = str(node_output.get("intent", route)).strip().lower()
                        if detected in {"direct", "multiagent"}:
                            route = detected
                        research_logger.update_content("intent", route)
                    if node_name == "plan":
                        research_logger.update_content("plan", str(node_output.get("plan", ""))[:200])
                    if node_name == "web_search":
                        research_logger.update_content("web_stats", node_output.get("web_retrieval_stats", {}))
                    if node_name == "local_rag":
                        research_logger.update_content("local_stats", node_output.get("local_retrieval_stats", {}))
                    if node_name == "deep_dive":
                        research_logger.update_content("evidence_pool", node_output.get("evidence_pool", [])[:10])
                    value = node_output.get("final")
                    if value:
                        final = str(value)
        if cancel_event and cancel_event.is_set():
            emit({"type": "status", "message": "任务已被用户中断"})
            return "", route
        if not final:
            snapshot = self._app.get_state(config)
            if snapshot.values.get("final"):
                final = str(snapshot.values["final"])
            if snapshot.next:
                # 有待执行的节点，说明任务被暂停
                if snapshot.interrupts:
                    for intr in snapshot.interrupts:
                        emit({
                            "type": "interrupt",
                            "interrupt_id": intr.id,
                            "node": intr.value.get("node", "") if isinstance(intr.value, dict) else "",
                            "value": intr.value,
                            "thread_id": runtime_config.thread_id,
                            "resumable": True,
                        })
                else:
                    emit({"type": "interrupted", "message": "任务已暂停，等待用户输入"})
            else:
                result = self._app.invoke(state, config)
                final = str(result.get("final", ""))
                route = str(result.get("intent", route)).strip().lower()
        if self._memory_manager and runtime_config.enable_memory and final:
            self._memory_manager.persist_turn(
                tenant_id=runtime_config.tenant_id,
                user_id=runtime_config.user_id,
                thread_id=runtime_config.thread_id,
                query=query,
                answer=final,
            )
        return final, route

    def resume(
        self,
        thread_id: str,
        resume_value: dict | str,
        emit: Callable[[dict], None] | None = None,
        cancel_event: Optional[Event] = None,
    ) -> tuple[str, str, bool]:
        """恢复被中断的任务。

        Args:
            thread_id: 被中断任务的线程 ID
            resume_value: 用户传入的恢复值（确认/修改/补充信息）
            emit: 事件回调
            cancel_event: 取消事件标志

        Returns:
            (final_text, route, interrupt_emitted)
            - final_text: 最终结果文本（如果有）
            - route: 执行路径（如 "multiagent"）
            - interrupt_emitted: 是否在 stream 中发出了新的 HITL interrupt 事件
        """
        self._ensure_initialized()
        t0 = time.time()
        config = {"configurable": {"thread_id": thread_id}}
        logger.info("[TRACE] resume START | thread=%s | resume_value_type=%s | resume_value=%s",
                     thread_id, type(resume_value).__name__,
                     str(resume_value)[:200] if resume_value else "(empty)")

        snapshot = self._app.get_state(config)
        logger.info("[TRACE] resume STATE | thread=%s | next=%s | interrupts=%d | has_final=%s",
                     thread_id,
                     list(snapshot.next) if snapshot.next else [],
                     len(snapshot.interrupts) if snapshot.interrupts else 0,
                     bool(snapshot.values.get("final")))
        if not snapshot.next:
            logger.info("[TRACE] resume EARLY-FINAL | thread=%s | final_len=%d",
                         thread_id, len(str(snapshot.values.get("final", ""))))
            return str(snapshot.values.get("final", "")), "completed"

        final = ""
        route = "multiagent"

        logger.info("[TRACE] resume STREAM-START | thread=%s", thread_id)
        node_count = 0
        interrupt_emitted = False  # 🔧 修复 #5：标记是否在 stream 中发出了 interrupt 事件
        for update in self._app.stream(Command(resume=resume_value), config, stream_mode="updates"):
            if not isinstance(update, dict):
                continue
            if "__interrupt__" in update:
                interrupts = update["__interrupt__"]
                interrupt_emitted = True
                logger.info("[TRACE] resume INTERRUPT | thread=%s | interrupt_count=%d", thread_id, len(interrupts))
                for intr in interrupts:
                    if emit:
                        emit({
                            "type": "interrupt",
                            "interrupt_id": intr.id,
                            "node": intr.value.get("node", "") if isinstance(intr.value, dict) else "",
                            "value": intr.value,
                            "thread_id": thread_id,
                            "resumable": True,
                        })
                break

            for node_name, node_output in update.items():
                node_count += 1
                logger.debug("[TRACE] resume NODE | thread=%s | node=%s | has_final=%s",
                              thread_id, node_name, isinstance(node_output, dict) and bool(node_output.get("final")))
                if cancel_event and cancel_event.is_set():
                    logger.info("[TRACE] resume CANCEL | thread=%s | node=%s", thread_id, node_name)
                    if emit:
                        emit({"type": "status", "message": "任务已被用户中断"})
                    return "", route
                if node_name == "__interrupt__":
                    continue
                if emit:
                    emit({"type": "phase", "node": node_name, "message": self._node_message(str(node_name))})
                    if isinstance(node_output, dict):
                        detail = self._node_detail_message(str(node_name), node_output)
                        if detail:
                            emit({"type": "status", "message": detail, "node": node_name})
                if isinstance(node_output, dict):
                    value = node_output.get("final")
                    if value:
                        final = str(value)

        logger.info("[TRACE] resume STREAM-END | thread=%s | nodes_run=%d | final_len=%d | interrupt_emitted=%s",
                     thread_id, node_count, len(final), interrupt_emitted)
        if not final:
            snapshot = self._app.get_state(config)
            final = str(snapshot.values.get("final", ""))
            logger.info("[TRACE] resume FINAL-FROM-SNAPSHOT | thread=%s | final_len=%d",
                         thread_id, len(final))

        logger.info("[TRACE] resume DONE | thread=%s | final_len=%d | elapsed=%.2fs",
                     thread_id, len(final), time.time() - t0)
        # 返回三元组新增的标记通过属性传递：前端可通过 stream_events 事件判断，
        # 这里改为 dict 以区分"已发 interrupt"和"真的无 final"
        return final, route, interrupt_emitted

    def get_state(self, thread_id: str) -> dict:
        """获取任务当前状态快照。"""
        self._ensure_initialized()
        config = {"configurable": {"thread_id": thread_id}}
        snapshot = self._app.get_state(config)
        return {
            "thread_id": thread_id,
            "values": {k: v for k, v in snapshot.values.items() if k in (
                "query", "phase", "intent", "iteration", "plan", "final",
                "hitl_enabled", "user_feedback",
            )},
            "next": list(snapshot.next) if snapshot.next else [],
            "interrupts": [
                {"id": intr.id, "value": intr.value}
                for intr in snapshot.interrupts
            ] if snapshot.interrupts else [],
            "created_at": snapshot.created_at,
            "parent_config": snapshot.parent_config,
        }

    def get_state_history(self, thread_id: str, limit: int = 20) -> list[dict]:
        """获取任务历史快照列表（时间旅行）。"""
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
        """手动更新任务状态。"""
        self._ensure_initialized()
        config = {"configurable": {"thread_id": thread_id}}
        self._app.update_state(config, values, as_node=as_node)
        return {"thread_id": thread_id, "updated": True}

    async def run(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None,
        enable_memory: bool | None,
        hitl_enabled: bool | None = None,
    ) -> str:
        final, _ = await asyncio.to_thread(
            self._run_sync,
            query,
            user_id,
            thread_id,
            tenant_id,
            max_iterations,
            enable_memory,
            hitl_enabled,
        )
        return final

    async def run_with_route(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None,
        enable_memory: bool | None,
        hitl_enabled: bool | None = None,
    ) -> tuple[str, str]:
        return await asyncio.to_thread(
            self._run_sync,
            query,
            user_id,
            thread_id,
            tenant_id,
            max_iterations,
            enable_memory,
            hitl_enabled,
        )

    def _has_resumable_task(self, thread_id: str) -> Optional[dict]:
        """检查该 thread_id 是否有可恢复的中断任务（HITL 或未完成的 multiagent）。"""
        try:
            config = {"configurable": {"thread_id": thread_id}}
            snapshot = self._app.get_state(config)
            if not snapshot or not snapshot.values:
                return None
            # 有未执行完的节点（如 HITL 中断或多智能体链路暂停）
            if snapshot.next:
                return {
                    "thread_id": thread_id,
                    "next": list(snapshot.next),
                    "interrupts": len(snapshot.interrupts) if snapshot.interrupts else 0,
                    "intent": snapshot.values.get("intent", ""),
                    "has_final": bool(snapshot.values.get("final")),
                }
            return None
        except Exception:
            return None

    async def stream_events(
        self,
        query: str,
        user_id: str,
        thread_id: str,
        tenant_id: str,
        max_iterations: int | None,
        enable_memory: bool | None,
        hitl_enabled: bool | None = None,
    ) -> AsyncIterator[dict]:
        logger.info("[TRACE] stream_events START | thread=%s | user=%s | query=%s",
                     thread_id, user_id, query[:120])

        # 🔧 修复 #2：若该 thread 已有未完成的多智能体链路（如 HITL 中断后用户切回），
        # 禁止再建新请求，强制走 /resume 路径。否则 LangGraph 会用全新 initial_state 覆盖旧进度，
        # 导致用户看到 "direct_answer" 直接出 "请继续" 这种无法恢复的输出。
        existing = self._has_resumable_task(thread_id)
        if existing and not existing.get("has_final"):
            logger.warning(
                "[TRACE] stream_events REJECTED | thread=%s 已有待恢复任务，请使用 /resume 接口 | next=%s",
                thread_id, existing.get("next"),
            )
            # 通过 SSE 向前端返回错误并终止，前端捕获后可自动改为 /resume
            err_event = {
                "type": "error",
                "code": "TASK_NEEDS_RESUME",
                "message": (
                    f"该会话有未完成的研究链路（下一节点：{existing.get('next')}），"
                    f"请点击「继续」以恢复，而非重复发起。"
                ),
                "resumable": True,
                "thread_id": thread_id,
            }
            async def _emit_error_once():
                yield err_event
                yield {"type": "__done__"}
            async for evt in _emit_error_once():
                yield evt
            return

        queue: asyncio.Queue[dict] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cancel_event = Event()
        self._cancel_flags[thread_id] = cancel_event

        # ── 落库会话记录：新建会话立刻出现在历史列表里，并自动命名 ──
        # 之前只在拿到 final 结果后才刷新列表，导致「新建会话后看不到历史在哪」。
        self._record_thread(thread_id, user_id, title=generate_thread_title(query))

        def emit(event: dict) -> None:
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)

        def worker() -> None:
            try:
                final, route = self._run_sync_with_events(
                    query=query,
                    user_id=user_id,
                    thread_id=thread_id,
                    tenant_id=tenant_id,
                    max_iterations=max_iterations,
                    enable_memory=enable_memory,
                    emit=emit,
                    hitl_enabled=hitl_enabled,
                    cancel_event=cancel_event,
                )
                if cancel_event.is_set():
                    logger.info("[TRACE] stream_events CANCEL | thread=%s", thread_id)
                    emit({"type": "cancelled", "message": "任务已取消"})
                elif final:
                    logger.info("[TRACE] stream_events FINAL | thread=%s | route=%s | final_len=%d",
                                 thread_id, route, len(final))
                    self._complete_thread(thread_id, intent=str(route or ""))
                    emit({"type": "route", "message": "已走直接回答路径" if route == "direct" else "已走多智能体研究路径"})
                    emit(
                        {
                            "type": "final",
                            "query": query,
                            "user_id": user_id,
                            "thread_id": thread_id,
                            "tenant_id": tenant_id,
                            "final": final,
                        }
                    )
                else:
                    logger.warning("[TRACE] stream_events NO-FINAL | thread=%s — stream completed without final output!", thread_id)
                    emit({"type": "error", "message": "研究链路异常结束：未产生最终结果，请重试或联系管理员"})
            except Exception as exc:
                logger.error("[TRACE] stream_events EXCEPTION | thread=%s | error=%s", thread_id, exc, exc_info=True)
                emit({"type": "error", "message": str(exc)})
            finally:
                self._cancel_flags.pop(thread_id, None)
                # 参考 gpt-researcher 的 JSONResearchHandler：保存研究日志
                close_research_logger(thread_id, route=route, final=final)
                emit({"type": "__done__"})

        Thread(target=worker, daemon=True).start()
        event_count = 0
        while True:
            event = await queue.get()
            event_count += 1
            if event.get("type") == "__done__":
                logger.info("[TRACE] stream_events END | thread=%s | events_yielded=%d", thread_id, event_count)
                break
            yield event

    def cancel_task(self, thread_id: str) -> bool:
        """取消正在运行的任务。"""
        flag = self._cancel_flags.get(thread_id)
        if flag:
            flag.set()
            logger.info("任务取消请求已发送 | thread_id=%s", thread_id)
            return True
        return False

    # ── 会话元数据（chat_threads 表）────────────────────────────────

    def _record_thread(
        self,
        thread_id: str,
        user_id: str,
        title: str = "",
        intent: str = "",
        completed: bool = False,
        message_delta: int = 1,
    ) -> None:
        """写入/更新会话记录（失败只记日志，不阻断主链路）。"""
        if self._thread_repo is None:
            return
        try:
            self._thread_repo.upsert_thread(
                thread_id=thread_id,
                user_id=user_id,
                title=title,
                intent=intent,
                completed=completed,
                message_delta=message_delta,
            )
        except Exception as exc:
            logger.warning("会话记录落库失败 | thread_id=%s | %s", thread_id, exc)

    def _complete_thread(self, thread_id: str, intent: str = "") -> None:
        """标记会话已产出最终结果。"""
        if self._thread_repo is None:
            return
        try:
            self._thread_repo.mark_completed(thread_id, intent=intent)
        except Exception as exc:
            logger.warning("会话完成标记失败 | thread_id=%s | %s", thread_id, exc)

    def list_threads(self, user_id: str, limit: int = 50, keyword: str = "") -> list[dict]:
        """
        列出用户的会话历史。

        数据源是独立的 chat_threads 表（置顶优先，其余按最近活跃时间倒序），
        不再去扫 LangGraph 的 checkpoints 表 —— 后者没有 user_id、
        且 checkpoint_id 是 UUID 无法按时间排序，会造成历史列表乱序。
        """
        self._ensure_initialized()
        if self._thread_repo is not None:
            try:
                return self._thread_repo.list_threads(
                    user_id=user_id, limit=limit, keyword=keyword
                )
            except Exception as exc:
                logger.warning("读取会话列表失败（降级到 checkpoints 扫描）: %s", exc)
        return self._list_threads_from_pg(user_id, limit)

    def rename_thread(self, thread_id: str, title: str, user_id: str) -> bool:
        """重命名会话。"""
        self._ensure_initialized()
        if self._thread_repo is None:
            return False
        return self._thread_repo.rename_thread(thread_id, title, user_id)

    def set_thread_pinned(self, thread_id: str, pinned: bool, user_id: str) -> bool:
        """置顶 / 取消置顶会话。"""
        self._ensure_initialized()
        if self._thread_repo is None:
            return False
        return self._thread_repo.set_pinned(thread_id, pinned, user_id)

    def delete_thread(self, thread_id: str, user_id: str) -> bool:
        """删除会话元数据（LangGraph checkpoint 保留，避免破坏可恢复状态）。"""
        self._ensure_initialized()
        if self._thread_repo is None:
            return False
        return self._thread_repo.delete_thread(thread_id, user_id)

    def _list_threads_from_pg(self, user_id: str, limit: int) -> list[dict]:
        """直接查 PG checkpoints 表获取所有会话线程。

        LangGraph PostgresSaver 的表结构:
          checkpoints(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata)
        """
        if not self._base_config or not self._base_config.postgres_dsn:
            return []
        if self._base_config.checkpointer_backend not in ("postgres", "auto"):
            return []
        try:
            import psycopg
            import json as _json
            conn = psycopg.connect(self._base_config.postgres_dsn)
            threads: list[dict] = []
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    # 获取所有 thread_id 的最新 checkpoint（checkpoint_ns='' 为根命名空间）
                    cur.execute(
                        """
                        SELECT DISTINCT ON (thread_id)
                            thread_id,
                            checkpoint_id,
                            metadata,
                            checkpoint
                        FROM checkpoints
                        WHERE checkpoint_ns = ''
                        ORDER BY thread_id, checkpoint_id DESC
                        LIMIT %s
                        """,
                        (limit,),
                    )
                    rows = cur.fetchall()
                    for row in rows:
                        tid = row[0]
                        metadata = row[2] or {}
                        checkpoint_data = row[3] or {}
                        # 从 checkpoint 中提取 state values
                        # LangGraph checkpoint 结构: {"channel_values": {...}, ...}
                        state_values = {}
                        if isinstance(checkpoint_data, dict):
                            state_values = checkpoint_data.get("channel_values", {})
                        elif isinstance(checkpoint_data, str):
                            try:
                                parsed = _json.loads(checkpoint_data)
                                state_values = parsed.get("channel_values", {}) if isinstance(parsed, dict) else {}
                            except Exception:
                                pass
                        # 处理 metadata 可能是 dict 或 str
                        if isinstance(metadata, str):
                            try:
                                metadata = _json.loads(metadata)
                            except Exception:
                                metadata = {}

                        query = ""
                        final = ""
                        intent = ""
                        # state_values 的值可能是 {"query": "...", "final": "..."}
                        if isinstance(state_values, dict):
                            query = str(state_values.get("query", ""))
                            final = str(state_values.get("final", ""))
                            intent = str(state_values.get("intent", ""))
                        # 从 metadata 中获取 created_at
                        created_at = ""
                        if isinstance(metadata, dict):
                            created_at = str(metadata.get("created_at", ""))
                        threads.append({
                            "thread_id": tid,
                            "query": query[:80] if query else "",
                            "intent": intent,
                            "completed": bool(final),
                            "created_at": created_at,
                        })
            finally:
                conn.close()
            return threads
        except Exception as exc:
            logger.warning("PG 查询 checkpoints 表失败 (可能表不存在): %s", exc)
            return []

    def _collect_all_thread_ids_memory(self) -> list[str]:
        """InMemory checkpointer 降级方案: 无法遍历所有 thread_id，返回空。"""
        return []

    def get_thread_messages(self, thread_id: str, limit: int = 100) -> list[dict]:
        """获取某个会话的完整对话历史。
        
        参考 memory-template 项目的 thread_id 会话隔离机制：
        从 LangGraph checkpoint 中恢复对话状态，确保切会话再切回不丢失。
        """
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
            # 如果有 final 结果，确保最后一个消息是 final
            final = snapshot.values.get("final", "")
            if final and (not messages or messages[-1]["content"] != final):
                messages.append({"role": "assistant", "content": final})
            
            # 🔧 修复会话切换后历史丢失：如果 messages 为空但有 query 和 final，
            # 至少返回用户问题和最终回答（参考 memory-template 的 fallback 机制）
            if not messages:
                query = snapshot.values.get("query", "")
                if query:
                    messages.append({"role": "user", "content": query})
                if final:
                    messages.append({"role": "assistant", "content": final})
                elif query:
                    # 任务未完成（如被中断），给出状态提示
                    next_nodes = list(snapshot.next) if snapshot.next else []
                    if next_nodes:
                        messages.append({"role": "assistant", "content": f"⏳ 研究进行中（下一步：{next_nodes[0]}），请点击「继续」恢复。"})
                    else:
                        messages.append({"role": "assistant", "content": "此会话暂无完成结果。"})
        except Exception as exc:
            logger.warning("获取会话消息失败: %s", exc)
        return messages

    async def resume_stream(
        self,
        thread_id: str,
        resume_value: dict | str,
    ) -> AsyncIterator[dict]:
        """流式恢复中断的任务，返回 SSE 事件流。"""
        logger.info("[TRACE] resume_stream START | thread=%s | resume_value=%s",
                     thread_id, str(resume_value)[:200] if resume_value else "(empty)")
        queue: asyncio.Queue[dict] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        cancel_event = Event()
        self._cancel_flags[thread_id] = cancel_event

        def emit(event: dict) -> None:
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)

        def worker() -> None:
            try:
                # resume() 返回 (final, route, interrupt_emitted)
                result = self.resume(thread_id, resume_value, emit=emit, cancel_event=cancel_event)
                final, route, interrupt_emitted = result[0], result[1], result[2]
                if cancel_event.is_set():
                    logger.info("[TRACE] resume_stream CANCEL | thread=%s", thread_id)
                    emit({"type": "cancelled", "message": "任务已取消"})
                elif final:
                    logger.info("[TRACE] resume_stream FINAL | thread=%s | final_len=%d", thread_id, len(final))
                    emit({"type": "route", "message": "任务已恢复"})
                    emit({"type": "final", "thread_id": thread_id, "final": final})
                elif interrupt_emitted:
                    # 🔧 修复 #5：resume 链路再次遇到 HITL interrupt（如 write_review）时，
                    # 已在 resume() 内通过 emit() 把 interrupt 事件推送给了前端。
                    # 此处不能报"无 final"错误 —— 前端需要停留等待用户输入。
                    logger.info("[TRACE] resume_stream HITL-WAIT | thread=%s — resume paused on new interrupt, waiting for user input", thread_id)
                    # 通知前端当前状态：等待用户响应，不是错误
                    emit({"type": "status", "message": "HITL_INTERRUPT_EMITTED", "resumable": True, "thread_id": thread_id})
                else:
                    logger.warning("[TRACE] resume_stream NO-FINAL | thread=%s — resume completed without final output!", thread_id)
                    emit({"type": "status", "message": "恢复完成但未获得最终结果，请重试"})
            except Exception as exc:
                logger.error("[TRACE] resume_stream EXCEPTION | thread=%s | error=%s", thread_id, exc, exc_info=True)
                emit({"type": "error", "message": str(exc)})
            finally:
                self._cancel_flags.pop(thread_id, None)
                emit({"type": "__done__"})

        Thread(target=worker, daemon=True).start()
        event_count = 0
        while True:
            event = await queue.get()
            event_count += 1
            if event.get("type") == "__done__":
                logger.info("[TRACE] resume_stream END | thread=%s | events_yielded=%d", thread_id, event_count)
                break
            yield event
