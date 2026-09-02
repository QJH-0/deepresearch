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
from mult_agents.runtime import build_checkpointer, build_memory_manager
from mult_agents.models import build_agents
from mult_agents.state import create_initial_state
from mult_agents.research_logger import get_research_logger, close_research_logger
from backend.schemas.events import event, sse, EventEnvelope
from backend.infra import ThreadRepository, generate_thread_title

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
        self._memory_manager = None
        self._app = None
        self._thread_repo: Optional[ThreadRepository] = None

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
    ) -> dict:
        memory_context = ""
        if self._memory_manager and runtime_config.enable_memory:
            memory_context = self._memory_manager.build_personalized_prompt_context(
                user_id=runtime_config.user_id,
                thread_id=runtime_config.thread_id,
                query=query,
                tenant_id=runtime_config.tenant_id,
                max_memories=runtime_config.memory_top_k,
            )
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
        input_state = self._build_initial_state(query, runtime_config)
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
                            yield sse(event("interrupt.raised",
                                            interrupt_id=intr.id,
                                            kind="plan_approval",
                                            payload=intr.value if isinstance(intr.value, dict) else {"value": intr.value}))
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

            # 2. 正常结束：发 run.completed
            if final:
                self._complete_thread(thread_id, intent=route)
                close_research_logger(thread_id, route=route, final=final)
                logger.info("[TRACE] stream_research DONE | run=%s | thread=%s | route=%s | final_len=%d | elapsed=%.2fs",
                             run_id, thread_id, route, len(final), time.time() - t0)
                yield sse(event("run.completed", message_id=f"{run_id}:write", final_state="done"))
            else:
                # 尝试从快照获取 final
                snapshot = self._app.get_state(config)
                final = str(snapshot.values.get("final", ""))
                if final:
                    self._complete_thread(thread_id, intent=route)
                    close_research_logger(thread_id, route=route, final=final)
                    yield sse(event("run.completed", message_id=f"{run_id}:write", final_state="done"))
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
        input_state = self._build_initial_state(query, runtime_config)
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

        if self._memory_manager and runtime_config.enable_memory and final:
            self._memory_manager.persist_turn(
                tenant_id=runtime_config.tenant_id,
                user_id=runtime_config.user_id,
                thread_id=runtime_config.thread_id,
                query=query,
                answer=final,
            )
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
        input_state = self._build_initial_state(query, runtime_config)
        config = {"configurable": {"thread_id": runtime_config.thread_id}}

        result = self._app.invoke(input_state, config)
        final = str(result.get("final", ""))
        route = str(result.get("intent", "multiagent")).strip().lower()

        if self._memory_manager and runtime_config.enable_memory and final:
            self._memory_manager.persist_turn(
                tenant_id=runtime_config.tenant_id,
                user_id=runtime_config.user_id,
                thread_id=runtime_config.thread_id,
                query=query,
                answer=final,
            )
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

    # ── 恢复（P3 重写，当前占位）──────────────────────

    async def resume_stream(self, thread_id: str, resume_value: dict | str) -> AsyncGenerator[str, None]:
        """流式恢复中断的任务。P3 阶段重写。"""
        self._ensure_initialized()
        config = {"configurable": {"thread_id": thread_id}}
        run_id = uuid.uuid4().hex[:12]

        yield sse(event("run.started", thread_id=thread_id, run_id=run_id))

        try:
            for update in self._app.stream(Command(resume=resume_value), config, stream_mode="updates"):
                if not isinstance(update, dict):
                    continue
                if "__interrupt__" in update:
                    for intr in update["__interrupt__"]:
                        yield sse(event("interrupt.raised",
                                        interrupt_id=intr.id,
                                        kind="plan_approval",
                                        payload=intr.value if isinstance(intr.value, dict) else {"value": intr.value}))
                    break
                for node_name, node_output in update.items():
                    if node_name == "__interrupt__":
                        continue
                    label = NODE_LABELS.get(node_name, node_name)
                    yield sse(event("agent.status", node=node_name, label=label, phase="completed"))
                    if isinstance(node_output, dict):
                        final = node_output.get("final")
                        if final:
                            yield sse(event("run.completed", message_id=f"{run_id}:write", final_state="done"))
                            return
            # 如果没有 final，尝试快照
            snapshot = self._app.get_state(config)
            final = str(snapshot.values.get("final", ""))
            if final:
                yield sse(event("run.completed", message_id=f"{run_id}:write", final_state="done"))
            else:
                yield sse(event("run.error", code="NoFinalOutput", message="恢复完成但未获得最终结果"))
        except Exception as e:
            yield sse(event("run.error", code=type(e).__name__, message=str(e)))


# ── 单例 ──────────────────────────────────────────

_SERVICE: ResearchService | None = None


def get_research_service() -> ResearchService:
    global _SERVICE
    if _SERVICE is None:
        import os
        config_path = os.getenv("CONFIG_PATH", "app/config.json")
        _SERVICE = ResearchService(config_path=config_path)
    return _SERVICE
