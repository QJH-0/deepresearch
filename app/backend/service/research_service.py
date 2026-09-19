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
import time
import uuid
from collections.abc import AsyncGenerator
from threading import Lock
from typing import Optional, AsyncIterator

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from mult_agents.config import AppConfig
from mult_agents.graph import build_app as build_workflow_app
from mult_agents.runtime import build_checkpointer, get_checkpointer, recursion_limit_for
from mult_agents.models import build_agents
from mult_agents.state import create_initial_state
from mult_agents.research_logger import get_research_logger, close_research_logger
from backend.schemas.events import event, sse
from backend.infra import ThreadRepository, generate_thread_title
from backend.service.memory_service import get_memory_service
from backend.service.summary_service import get_summary_service


async def _generate_llm_title(query: str, report_summary: str, api_key: str) -> str:
    """P6-6: 用轻量模型从用户问题+报告摘要生成 ≤20 字标题。"""
    from backend.config.settings import get_business_settings
    from mult_agents.models import build_aux_llm

    biz = get_business_settings()
    llm = build_aux_llm(
        biz.title_model,
        api_key,
        timeout=biz.llm_timeout_seconds,
        max_retries=biz.llm_max_retries,
    )
    prompt = (
        f"请根据以下用户提问和研究报告摘要，生成一个不超过20个字的简洁中文标题。\n"
        f"只输出标题文字，不要引号、不要标点。\n\n"
        f"用户提问：{query[:200]}\n"
        f"报告摘要：{report_summary[:500]}"
    )
    try:
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        title = resp.content.strip().strip('"\'').strip()
        if title and len(title) <= 30:
            return title
        return ""
    except Exception as exc:
        logger.warning("LLM 标题生成失败: %s", exc)
        return ""

logger = logging.getLogger("backend.research_service")

HEARTBEAT_FRAME = ": ping\n\n"


def _get_heartbeat_interval() -> float:
    """从 BusinessSettings 读取 SSE 心跳间隔（秒），<=0 表示关闭。"""
    try:
        from backend.config.settings import get_business_settings
        return float(get_business_settings().sse_heartbeat_seconds)
    except Exception:
        return 15.0


async def _astream_with_heartbeat(
    astream_iter: AsyncIterator,
    interval: float,
):
    """逐项产出 astream chunk；超过 interval 秒无产出时先产出心跳标记。

    产出物为 ("heartbeat", None) 或 (mode, chunk)，调用方据此 yield 心跳注释帧。
    超时后不取消内部 future，避免杀掉正在执行的 graph step。
    """
    if interval <= 0:
        async for mode, chunk in astream_iter:
            yield mode, chunk
        return

    aiter = astream_iter.__aiter__()
    _sentinel = object()
    pending: asyncio.Future | None = None

    while True:
        if pending is None:
            try:
                pending = asyncio.ensure_future(anext(aiter))
            except StopAsyncIteration:
                return

        try:
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if pending in done:
                try:
                    mode, chunk = pending.result()
                except StopAsyncIteration:
                    pending = None
                    return
                pending = None
                yield mode, chunk
            else:
                yield "heartbeat", None
        except asyncio.CancelledError:
            if pending is not None and not pending.done():
                pending.cancel()
            raise

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



class _StreamTranslator:
    """把 graph.astream 的 (mode, chunk) 翻译成 SSE 帧。

    stream_research 与 resume_stream 共用同一套翻译逻辑 —— 两条入口此前各写一份
    近乎相同的实现，已经出现过「一边补了旧格式兼容分支、另一边没补」的分歧。
    可变状态（seen_nodes / last_token_node / final / route）由实例持有，
    调用方只需把 translate() 的返回值逐个 yield 出去。
    """

    def __init__(self, run_id: str, research_logger=None):
        self.run_id = run_id
        self.research_logger = research_logger
        self.seen_nodes: set[str] = set()
        self.last_token_node = ""
        self.final = ""
        self.route = "multiagent"

    def translate(self, mode: str, chunk) -> list[str]:
        """返回该 chunk 应发出的 SSE 帧列表（可能为空）。"""
        if mode == "custom":
            return self._custom_frames(chunk)
        if mode == "updates":
            return self._update_frames(chunk)
        return []

    # ── custom 通道：节点内 StreamWriter 发出的自定义事件 ──

    def _custom_frames(self, chunk) -> list[str]:
        if not isinstance(chunk, dict):
            return []

        evt_type = chunk.get("type", "")
        frames: list[str] = []

        if evt_type == "token":
            frames.extend(self._start_message_if_needed(chunk.get("node", "")))
            frames.append(sse(event("message.delta", message_id=self._message_id(chunk), text=chunk.get("text", ""))))
        elif evt_type == "thinking":
            frames.extend(self._start_message_if_needed(chunk.get("node", "")))
            frames.append(sse(event("message.thinking", message_id=self._message_id(chunk), text=chunk.get("text", ""))))
        elif evt_type == "progress":
            frames.append(self._status_frame(chunk.get("node", ""), "running"))
        elif "node" in chunk and "message" in chunk and "type" not in chunk:
            # 旧格式兼容：{node: "...", message: "..."}
            # message 是节点自己写的进度文案，透传为 detail 供过程卡片展示
            frames.append(
                self._status_frame(chunk.get("node", ""), "running", chunk.get("message", ""))
            )
        elif evt_type == "sources":
            frames.append(sse(event("sources.found", sources=chunk.get("sources", []))))

        return frames

    def _message_id(self, chunk: dict) -> str:
        node = chunk.get("node", "")
        self.last_token_node = node
        return f"{self.run_id}:{node}"

    def _start_message_if_needed(self, node: str) -> list[str]:
        if node in self.seen_nodes:
            return []
        self.seen_nodes.add(node)
        return [sse(event("message.start", message_id=f"{self.run_id}:{node}", node=node))]

    @staticmethod
    def _status_frame(node: str, phase: str, detail: str = "") -> str:
        return sse(
            event(
                "agent.status",
                node=node,
                label=NODE_LABELS.get(node, node),
                phase=phase,
                detail=detail,
            )
        )

    # ── updates 通道：节点完成与 interrupt ──

    def _update_frames(self, chunk) -> list[str]:
        if not isinstance(chunk, dict):
            return []

        if "__interrupt__" in chunk:
            frames = []
            for intr in chunk["__interrupt__"]:
                intr_value = intr.value if isinstance(intr.value, dict) else {"value": intr.value}
                intr_kind = intr_value.get("kind", "unknown")
                frames.append(sse(event(
                    "interrupt.raised",
                    interrupt_id=intr.id,
                    kind=intr_kind,
                    payload=intr_value,
                )))
            return frames

        frames = []
        for node_name, node_output in chunk.items():
            if node_name == "__interrupt__":
                continue

            frames.append(self._status_frame(node_name, "completed"))

            if not isinstance(node_output, dict):
                continue

            if self.research_logger is not None:
                self.research_logger.log_event("node_complete", {"node": node_name})

            if node_name == "intent":
                detected = str(node_output.get("intent", self.route)).strip().lower()
                if detected in {"direct", "multiagent"}:
                    self.route = detected
                if self.research_logger is not None:
                    self.research_logger.update_content("intent", self.route)

            value = node_output.get("final")
            if value:
                self.final = str(value)

        return frames


class ResearchService:
    """研究服务：管理图执行、流式输出与会话元数据。

    替代旧 WorkflowService 的核心流式逻辑，不含后台线程/队列。
    """

    def __init__(self):
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
            base_config = AppConfig.from_file()
            agents = build_agents(base_config.model, base_config.api_key, base_config)
            # P2-2: 优先复用 lifespan 初始化的异步 checkpointer（AsyncPostgresSaver），
            # 未初始化（测试/独立调用）则降级到同步工厂（内存）。
            checkpointer = get_checkpointer() or build_checkpointer(base_config)
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
            max_retrieval_rounds=runtime_config.max_retrieval_rounds,
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
            max_retrieval_rounds=runtime_config.max_retrieval_rounds,
        )
        # R4.4: 用户真实输入写入 chat_messages（前端可见）
        input_state["chat_messages"] = [HumanMessage(content=query)]
        input_state["agent_messages"] = []
        config = {
            "configurable": {"thread_id": runtime_config.thread_id},
            # 按循环上限推算：内层重检会把超步数放大到默认 25 以上
            "recursion_limit": recursion_limit_for(
                runtime_config.max_iterations, runtime_config.max_retrieval_rounds
            ),
        }

        # 对话摘要压缩：从 checkpoint 获取已有消息 + 新 query 合并判断
        input_state = await self._apply_summary_if_needed(
            input_state, config, runtime_config
        )

        # 落库会话记录
        self._record_thread(thread_id, user_id, title=generate_thread_title(query))

        # 研究日志
        research_logger = get_research_logger(thread_id)
        research_logger.log_event("task_start", {"query": query, "thread_id": thread_id})
        research_logger.update_content("query", query)

        final = ""
        route = "multiagent"

        # 1. 发送 run.started
        yield sse(event("run.started", thread_id=thread_id, run_id=run_id))

        translator = _StreamTranslator(run_id, research_logger=research_logger)
        heartbeat_interval = _get_heartbeat_interval()
        # 看门狗：llm_timeout_seconds 只约束单次调用，一轮研究有 15~25 次 LLM 调用
        # 加十余次检索，任何一处慢下来都会让前端一直转圈。0 表示不限制。
        run_deadline = getattr(runtime_config, "run_timeout_seconds", 0) or None
        try:
            async with asyncio.timeout(run_deadline):
                async for mode, chunk in _astream_with_heartbeat(
                    self._app.astream(
                        input_state, config, stream_mode=["custom", "updates"]
                    ),
                    heartbeat_interval,
                ):
                    if mode == "heartbeat":
                        yield HEARTBEAT_FRAME
                        continue
                    for frame in translator.translate(mode, chunk):
                        yield frame

            final = translator.final
            route = translator.route
            last_token_node = translator.last_token_node

            # 2. 正常结束：发 run.completed + P5 后台记忆提取
            if final:
                # R4.4: 最终报告写入 chat_messages（前端可见）
                from langchain_core.messages import AIMessage as _AIM
                await self._app.aupdate_state(config, {"chat_messages": [_AIM(content=final)]})
                self._complete_thread(thread_id, intent=route)
                close_research_logger(thread_id, route=route, final=final)
                logger.info("[TRACE] stream_research DONE | run=%s | thread=%s | route=%s | final_len=%d | elapsed=%.2fs",
                             run_id, thread_id, route, len(final), time.time() - t0)
                yield sse(event("run.completed", message_id=f"{run_id}:{last_token_node or 'write'}", final_state="done", final=final))
                # P5: 后台记忆提取（run.completed 后异步触发，不阻塞）
                self._trigger_memory_extract(runtime_config, query, final, thread_id)
                # P6-6: LLM 标题生成（run.completed 后异步，不阻塞）
                self._trigger_title_gen(runtime_config, query, final, thread_id)
            else:
                # 尝试从快照获取 final
                snapshot = await self._app.aget_state(config)
                final = str(snapshot.values.get("final", ""))
                if final:
                    # R4.4: 最终报告写入 chat_messages
                    from langchain_core.messages import AIMessage as _AIM
                    await self._app.aupdate_state(config, {"chat_messages": [_AIM(content=final)]})
                    self._complete_thread(thread_id, intent=route)
                    close_research_logger(thread_id, route=route, final=final)
                    yield sse(event("run.completed", message_id=f"{run_id}:{last_token_node or 'write'}", final_state="done", final=final))
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

        except TimeoutError:
            # 看门狗触发。不标记 completed —— 这一轮没跑完；检查点保持完好，
            # 前端可据 /state 的 next_nodes 续研（与进程重启中断同一套恢复语义）。
            #
            # 必须记下超时时刻在跑哪个节点：否则运维只能看到「超时了」，
            # 分不清是「慢但在干活」还是「卡死了」，也就无从判断该调大上限还是查故障。
            logger.error(
                "[TRACE] stream_research TIMEOUT | run=%s | thread=%s | limit=%.0fs | last_node=%s",
                run_id, thread_id, run_deadline or 0,
                translator.last_token_node or "(unknown)",
            )
            close_research_logger(thread_id, route=route, final=final)
            yield sse(event(
                "run.error",
                code="RunTimeout",
                message=f"研究超过 {run_deadline:.0f} 秒未完成，已中止；可从检查点续研",
            ))

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
            max_retrieval_rounds=runtime_config.max_retrieval_rounds,
        )
        # R4.4: 用户真实输入写入 chat_messages（前端可见）
        input_state["chat_messages"] = [HumanMessage(content=query)]
        input_state["agent_messages"] = []
        config = {
            "configurable": {"thread_id": runtime_config.thread_id},
            # 按循环上限推算：内层重检会把超步数放大到默认 25 以上
            "recursion_limit": recursion_limit_for(
                runtime_config.max_iterations, runtime_config.max_retrieval_rounds
            ),
        }

        # 对话摘要压缩
        input_state = await self._apply_summary_if_needed(
            input_state, config, runtime_config
        )

        try:
            result = await self._app.ainvoke(input_state, config)
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
            max_retrieval_rounds=runtime_config.max_retrieval_rounds,
        )
        # R4.4: 用户真实输入写入 chat_messages
        input_state["chat_messages"] = [HumanMessage(content=query)]
        input_state["agent_messages"] = []
        config = {
            "configurable": {"thread_id": runtime_config.thread_id},
            # 按循环上限推算：内层重检会把超步数放大到默认 25 以上
            "recursion_limit": recursion_limit_for(
                runtime_config.max_iterations, runtime_config.max_retrieval_rounds
            ),
        }

        # 对话摘要压缩
        input_state = await self._apply_summary_if_needed(
            input_state, config, runtime_config
        )

        result = await self._app.ainvoke(input_state, config)
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

    async def _trigger_memory_extract_from_snapshot(
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
            snapshot = await self._app.aget_state(config)
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

    def get_thread_owner(self, thread_id: str) -> str | None:
        """返回会话归属用户；无记录或存储不可用时返回 None。

        调用方据此判断归属：None 表示「无归属记录」，与「归属他人」是两种
        不同情形，不可合并处理 —— 前者允许新建会话，后者必须拒绝。
        """
        self._ensure_initialized()
        if self._thread_repo is None:
            return None
        try:
            return self._thread_repo.get_thread_owner(thread_id)
        except Exception as exc:
            logger.warning("读取会话归属失败 | thread_id=%s | %s", thread_id, exc)
            return None

    async def _apply_summary_if_needed(
        self,
        input_state: dict,
        config: dict,
        runtime_config: AppConfig,
    ) -> dict:
        """在 graph 执行前对已有对话消息执行摘要压缩。

        R4.4: 从 chat_messages（用户可见对话）读取，不含内部推理 prompt。
        """
        summary_service = get_summary_service()
        if summary_service is None:
            return input_state

        try:
            snapshot = await self._app.aget_state(config)
            # R4.4: 优先读 chat_messages，兼容旧 checkpoint 的 messages
            existing_msgs = list(snapshot.values.get("chat_messages") or snapshot.values.get("messages") or [])
            existing_summary = str(snapshot.values.get("conversation_summary", ""))
        except Exception:
            existing_msgs = []
            existing_summary = ""

        new_msg = HumanMessage(content=input_state.get("query", ""))
        all_msgs = existing_msgs + [new_msg]

        if len(all_msgs) <= summary_service._threshold:
            input_state["conversation_summary"] = existing_summary
            return input_state

        compressed_msgs, new_summary = await summary_service.summarize_if_needed(
            all_msgs, existing_summary
        )

        input_state["conversation_summary"] = new_summary
        input_state["chat_messages"] = compressed_msgs

        await self._app.aupdate_state(config, {
            "chat_messages": compressed_msgs,
            "conversation_summary": new_summary,
        })

        return input_state

    async def _apply_summary_to_checkpoint(self, config: dict) -> None:
        """对 checkpoint 中已有对话消息执行摘要压缩（resume_stream 用）。

        R4.4: 从 chat_messages 读取，不含内部推理 prompt。
        """
        summary_service = get_summary_service()
        if summary_service is None:
            return

        try:
            snapshot = await self._app.aget_state(config)
            existing_msgs = list(snapshot.values.get("chat_messages") or snapshot.values.get("messages") or [])
            existing_summary = str(snapshot.values.get("conversation_summary", ""))
        except Exception:
            return

        if len(existing_msgs) <= summary_service._threshold:
            return

        compressed_msgs, new_summary = await summary_service.summarize_if_needed(
            existing_msgs, existing_summary
        )

        await self._app.aupdate_state(config, {
            "chat_messages": compressed_msgs,
            "conversation_summary": new_summary,
        })
        logger.info(
            "[summary] resume_stream 摘要压缩完成 | msgs %d -> %d",
            len(existing_msgs), len(compressed_msgs),
        )

    async def get_state(self, thread_id: str) -> dict:
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
        snapshot = await self._app.aget_state(config)

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
            # 有待执行节点但不在运行 → 可能是崩溃中断，router 层异步检查 interrupted_by_restart
            status = "idle"
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
        snapshot = await self._app.aget_state(config)

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

    async def get_state_history(self, thread_id: str, limit: int = 20) -> list[dict]:
        self._ensure_initialized()
        config = {"configurable": {"thread_id": thread_id}}
        history = []
        try:
            async for snapshot in self._app.aget_state_history(config, limit=limit):
                history.append({
                    "checkpoint_id": snapshot.config.get("configurable", {}).get("checkpoint_id", ""),
                    "next": list(snapshot.next) if snapshot.next else [],
                    "created_at": snapshot.created_at,
                    "interrupts_count": len(snapshot.interrupts) if snapshot.interrupts else 0,
                })
        except Exception as exc:
            logger.warning("获取状态历史失败 | thread=%s | %s", thread_id, exc)
        return history

    async def update_state(self, thread_id: str, values: dict, as_node: str | None = None) -> dict:
        self._ensure_initialized()
        config = {"configurable": {"thread_id": thread_id}}
        await self._app.aupdate_state(config, values, as_node=as_node)
        return {"thread_id": thread_id, "updated": True}

    async def get_thread_messages(self, thread_id: str, limit: int = 100) -> list[dict]:
        self._ensure_initialized()
        messages = []
        try:
            config = {"configurable": {"thread_id": thread_id}}
            snapshot = await self._app.aget_state(config)
            if not snapshot or not snapshot.values:
                return []
            # R4.4: 优先读 chat_messages（用户可见对话），兼容旧 checkpoint
            state_msgs = snapshot.values.get("chat_messages")
            if state_msgs is None:
                # 旧 checkpoint 无 chat_messages → 从 query + final 恢复
                query = snapshot.values.get("query", "")
                final = snapshot.values.get("final", "")
                if query:
                    messages.append({"role": "user", "content": query})
                if final:
                    messages.append({"role": "assistant", "content": final})
                return messages
            for msg in state_msgs[-limit:]:
                role = getattr(msg, "type", "unknown")
                content = getattr(msg, "content", str(msg))
                if role == "human":
                    messages.append({"role": "user", "content": content})
                elif role == "ai":
                    messages.append({"role": "assistant", "content": content})
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

        三种模式复用同一个 checkpoint 的 state，差别只在传给 astream 的输入：
        - mode=continue: 崩溃续研，输入 None → 从最后 checkpoint 的节点续跑
          （已检索的 sources/findings 全部保留，已完成节点不重跑）
        - mode=answer: HITL 回答，输入 Command(resume=resume_value) → 从 interrupt()
          调用处继续（resume_value 由 router 按 interrupt kind 校验）
        - mode=modify: 用户补充/修改条件，输入 Command(update=..., goto="intent") →
          追加 HumanMessage 后从入口重跑（旧检索数据仍在 checkpoint 中，流程按新条件重算）

        Args:
            thread_id: 会话 ID
            resume_value: HITL 回答值（mode=answer 时必填）；
                          mode=modify 时为用户补充的文本消息
            mode: "continue" | "answer" | "modify"
        """
        self._ensure_initialized()
        base_config = self._base_config
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": recursion_limit_for(
                base_config.max_iterations if base_config else 3,
                base_config.max_retrieval_rounds if base_config else 2,
            ),
        }
        run_id = uuid.uuid4().hex[:12]
        final = ""

        # 对话摘要压缩：恢复前检查 checkpoint 中已有消息
        await self._apply_summary_to_checkpoint(config)

        logger.info("[TRACE] resume_stream START | run=%s | thread=%s | mode=%s", run_id, thread_id, mode)

        yield sse(event("run.started", thread_id=thread_id, run_id=run_id))

        if mode == "continue":
            input_state = None
        elif mode == "answer":
            if resume_value is None:
                yield sse(event("run.error", code="InvalidResume",
                                message="mode=answer 必须提供 resume_value"))
                return
            input_state = Command(resume=resume_value)
        elif mode == "modify":
            modify_text = str(resume_value or "").strip()
            if not modify_text:
                yield sse(event("run.error", code="InvalidResume",
                                message="mode=modify 必须提供补充/修改文本"))
                return
            # update 里只重置覆盖型字段：累加型（findings/evidence）走 operator.add，
            # 无法用 update 清空，保留下来正好供新一轮复用。
            input_state = Command(
                update={
                    "chat_messages": [HumanMessage(content=modify_text)],
                    "query": modify_text,
                    "plan": "",
                    "draft": "",
                    "final": "",
                    "analysis": "",
                    "iteration": 0,
                    "needs_more_research": False,
                    "phase": "initialized",
                },
                goto="intent",
            )
        else:
            yield sse(event("run.error", code="InvalidResume", message=f"未知 mode: {mode}"))
            return

        translator = _StreamTranslator(run_id, research_logger=get_research_logger(thread_id))
        heartbeat_interval = _get_heartbeat_interval()
        try:
            async for mode_chunk, chunk in _astream_with_heartbeat(
                self._app.astream(
                    input_state, config, stream_mode=["custom", "updates"]
                ),
                heartbeat_interval,
            ):
                if mode_chunk == "heartbeat":
                    yield HEARTBEAT_FRAME
                    continue
                for frame in translator.translate(mode_chunk, chunk):
                    yield frame

            final = translator.final
            last_token_node = translator.last_token_node

            # 尝试获取 final
            if final:
                # R4.4: 最终报告写入 chat_messages
                from langchain_core.messages import AIMessage as _AIM
                await self._app.aupdate_state(config, {"chat_messages": [_AIM(content=final)]})
                self._complete_thread(thread_id, intent="multiagent")
                close_research_logger(thread_id, route="multiagent", final=final)
                logger.info("[TRACE] resume_stream DONE | run=%s | thread=%s | final_len=%d",
                             run_id, thread_id, len(final))
                yield sse(event("run.completed", message_id=f"{run_id}:{last_token_node or 'write'}", final_state="done", final=final))
                # P5: 后台记忆提取
                await self._trigger_memory_extract_from_snapshot(thread_id, final, config)
            else:
                snapshot = await self._app.aget_state(config)
                final = str(snapshot.values.get("final", ""))
                if final:
                    # R4.4: 最终报告写入 chat_messages
                    from langchain_core.messages import AIMessage as _AIM
                    await self._app.aupdate_state(config, {"chat_messages": [_AIM(content=final)]})
                    self._complete_thread(thread_id, intent="multiagent")
                    close_research_logger(thread_id, route="multiagent", final=final)
                    yield sse(event("run.completed", message_id=f"{run_id}:{last_token_node or 'write'}", final_state="done", final=final))
                    # P5: 后台记忆提取
                    await self._trigger_memory_extract_from_snapshot(thread_id, final, config)
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
        _SERVICE = ResearchService()
    return _SERVICE
