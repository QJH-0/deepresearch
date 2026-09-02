# Phase 2 · 流式输出重写 · 开发文档

> 依据：《DeepResearch_重构详细计划.md》v1.2 第三节、第五节 5.1、第六节 Phase 2
> 工期：2 天｜前置依赖：Phase 1 验收通过（新 State、nodes 包、事件 schema）｜后续依赖本 Phase 的：P3（TaskRegistry 取消挂在 generator 上）、P6（前端消费事件流）
> 参考仓库：`open_deep_research`（流式消费，commit `1b7d2e8`）、`ai-chatbot`（vercel，`app/(chat)/api/chat/route.ts`）

---

## 1. 目标与范围

**结论先行**：用纯 **async generator + `graph.astream(stream_mode=["messages-tuple","updates","custom"])`** 替换现有"后台 Thread + asyncio.Queue 桥接"实现，实现 token 级流式直通 SSE，并以三不变式（流必结束 / delta 顺序拼接完整 / 前端忽略未知 type）作为结构性异常兜底。现有 `workflow_service.py`（921 行）的核心流式部分（约 549-652 行）推倒重写为 `research_service.py`。

**范围边界**：
- ✅ 做：research_service.py 重写、message.delta token 直通、异常兜底与必发结束事件测试
- ❌ 不做：取消/断点续研（P3）、interrupt（P4）、前端改动（P6——旧前端本期可能暂时看不到 token 流，以 curl 验证 SSE 为准）

## 2. 现状锚点（已核实）

| 问题 | 位置 | 说明 |
|------|------|------|
| Thread+Queue 桥接 | `workflow_service.py:601-642`（worker 线程 + emit→queue + 主协程消费） | bug 温床：跨线程传事件、取消语义混乱 |
| finally NameError 挂起 | `workflow_service.py:638-642` | finally 块引用 try 内局部变量，异常路径抛 NameError 且 `__done__` 不再发出 → **前端流永久挂起** |
| 非 token 级流式 | `workflow_service.py:549-652` + `nodes.py:178,184` | custom 事件只推进度文案，报告一次性整块下发 |
| 取消只在节点间隙生效 | `workflow_service.py:278,308,411` | cancel_event 轮询，节点内 LLM 调用无法中断（P3 一并解决，本 Phase 先保证 CancelledError 传播路径通畅） |

## 3. 目标实现（详细设计）

### 3.1 服务层骨架

```python
# app/backend/service/research_service.py（重写核心）
from collections.abc import AsyncGenerator
from backend.schemas.events import event
from mult_agents.models import build_graph   # P1 产物

async def stream_research(graph, input_state: dict, thread_id: str) -> AsyncGenerator[str, None]:
    """纯 async generator，直接挂 StreamingResponse；无后台线程、无队列。"""
    config = {"configurable": {"thread_id": thread_id}}
    run_id = uuid4().hex
    final_message_id: str | None = None

    yield sse(event("run.started", thread_id=thread_id, run_id=run_id))

    try:
        async for mode, chunk in graph.astream(
            input_state, config, stream_mode=["messages-tuple", "updates", "custom"]
        ):
            match mode:
                case "messages-tuple":
                    # chunk = (AIMessageChunk, metadata) → message.delta
                    msg_chunk, metadata = chunk
                    node = metadata.get("langgraph_node", "")
                    mid = msg_id(node, run_id)
                    if msg_chunk.content:
                        yield sse(event("message.delta", message_id=mid, text=msg_chunk.content))
                case "updates":
                    # chunk = {node_name: output_dict} → agent.status
                    for node, output in chunk.items():
                        yield sse(event("agent.status", node=node, label=LABELS.get(node, node),
                                        phase="completed"))
                case "custom":
                    # 节点 StreamWriter 自定义事件 → sources.found / message.thinking 等
                    yield sse(translate_custom(chunk))   # P0 events.py 已定义的事件类型
        yield sse(event("run.completed", message_id=final_message_id, final_state="done"))

    except asyncio.CancelledError:
        # 用户取消（P3 TaskRegistry 触发）—— 结束事件在本分支发出后重新抛出
        yield sse(event("run.cancelled", reason="user_cancelled"))
        raise
    except Exception as e:
        # 任何异常必发 run.error，随后自然关闭 generator
        yield sse(event("run.error", code=type(e).__name__, message=str(e)))
    # ⚠️ 无 finally —— 不引用 try 块局部变量（结构性修复 workflow_service.py:641 NameError）
```

**关键点**：
1. **`messages-tuple` 模式天然 token 级**：LangGraph 直接给出 `(AIMessageChunk, metadata)`，服务层只做事件转换，不做任何拼接。
2. **服务层是纯转换器**：`research_service.py` 不含业务逻辑，业务在图节点内；`StreamingResponse(media_type="text/event-stream")` 直接消费本 generator。
3. **LLM 客户端开启 `streaming=True`**（ChatTongyi/dashscope 支持），否则 messages-tuple 只有一个大 chunk。
4. **message_id 生成规则**：`{run_id}:{node}:{seq}`，同一节点多轮输出递增 seq；`message.start` 事件在 updates 模式该节点首次出现时发出（或节点内 custom 事件显式发）。
5. **彻底删除**：`workflow_service.py` 的 `_run_sync_with_events`（196 行起）、`stream_events`（549 行起）、worker/queue 桥接全部删除；`resume_stream`（868 行起）在 P3 重写。

### 3.2 SSE 格式化

```python
def sse(envelope: EventEnvelope) -> str:
    return f"data: {envelope.model_dump_json()}\n\n"
```

router 层（`research_router.py:54` 的 `/stream`）改为：

```python
@router.post("/stream")
async def stream(req: ResearchRequest):
    gen = research_service.stream_research(...)
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```

### 3.3 节点侧 custom 事件（配合改动）

- 节点内通过 LangGraph `StreamWriter`（P1 拆包时保留的 `emit` 辅助）发自定义事件：`sources.found`（检索节点）、`message.thinking`（各节点 LLM 推理增量——若模型支持 reasoning 输出）。
- custom 事件的 payload 必须已是 P0 events.py 定义的 data 模型可构造的 dict（translate_custom 只包 envelope，不加工字段）。

### 3.4 三不变式的实现保证

| 不变式 | 实现保证 | 测试锚点 |
|--------|----------|----------|
| 流一定结束 | completed/cancelled/error 在各自分支内发出；generator 自然终止关闭 SSE；**无 finally** | T2-3 异常注入后断言最后一条事件是 run.error 且连接关闭 |
| delta 顺序拼接完整 | astream 顺序消费顺序 yield，无缓冲重排 | T2-2 收集全部 message.delta 拼接 == 完整报告文本 |
| 前端忽略未知 type | P6 前端 reducer default 分支静默跳过（协议层约定，本期文档冻结） | P6 验收覆盖 |

## 4. 任务分解

### P2-0 qwen 流式分块最小验证（第一天必做，计划第八节风险项）

写 10 行最小脚本确认 qwen 流式 API 在 `messages-tuple` 模式下的分块行为：

```python
# scripts/verify_stream_chunk.py —— 先于一切铺开工作
# 验证：1) ChatTongyi(streaming=True) 在 astream messages-tuple 下逐 token 出 chunk
#       2) chunk.content 非空且为增量；3) metadata 含 langgraph_node
```

若分块异常（整块下发）：排查 dashscope SDK 版本与 `streaming=True` 是否传到节点绑定的模型实例（P1-3 模型工厂产出），必要时节点绑定处统一 `.bind(streaming=True)`。**该结论回写本文档第 7 节。**

### P2-1 research_service.py 重写

- 按第 3 节实现；`workflow_service.py` 中线程管理相关（`_cancel_flags`、worker）删除，thread CRUD（`_record_thread`/`list_threads` 等 665-737 行）**保留**并迁入 research_service 或独立 `thread_service.py`（保持既有 REST 行为）。
- 旧 `workflow_service.py` 删除（一个方向一个权威实现）。

### P2-2 token 级直通

- messages-tuple → message.delta 映射；同一 message 的 message.start 只发一次（节点内首 chunk 时发，服务层维护 `seen_nodes` 集合）。
- 中间节点（deep_dive/analyze 等）的 LLM 输出也走 delta 直通（`node` 字段区分归属），前端据此渲染中间结论时间线（P6 AgentTimeline 消费）。

### P2-3 异常兜底

- 三不变式落地 + "必发结束事件"测试（见 T2-3/T2-4）。
- router 层加兜底：`StreamingResponse` 外再包一层（generator 本身抛 CancelledError 时 FastAPI 会静默关闭——确保 run.cancelled 在 generator 内已 yield）。

## 5. 测试计划

| 用例 | 类型 | 断言 |
|------|------|------|
| T2-1 分块行为验证 | 脚本 | scripts/verify_stream_chunk.py 输出：chunk 数 > 10、单 chunk content 长度 < 50（近似 token 级） |
| T2-2 delta 完整性 | 集成 | curl -N POST /stream 全程收集；全部 message.delta 按序拼接 == run.completed.message_id 对应报告全文 |
| T2-3 节点异常注入 | 集成 | 测试桩：让 analyze 节点抛 RuntimeError → SSE 流末条事件为 run.error（code=RuntimeError），流正常关闭不挂起（**对 workflow_service.py:641 的回归测试**） |
| T2-4 结束事件必达 | 集成 | 正常/异常/取消（P3 后）三种路径下，事件流最后一条 ∈ {run.completed, run.error, run.cancelled} |
| T2-5 事件协议合规 | 集成 | 捕获全部 SSE 行，逐条过 `EventEnvelope` + `EVENT_REGISTRY[type]` 校验（未知 type 记录但不失败——向后兼容验证） |
| T2-6 3 并发压测 | 集成 | 3 个不同 thread_id 并发 /stream，各自 delta 拼接报告互不串流（message_id 前缀 run_id 隔离） |
| T2-7 中间节点事件 | 集成 | agent.status 事件覆盖全部执行节点，顺序符合拓扑（intent → plan → … → write） |

## 6. 验收清单

- [ ] P2-0 分块验证脚本结论已记录（qwen token 级分块 OK 或已修复）
- [ ] `workflow_service.py` Thread+Queue 桥接彻底删除，`research_service.py` 成为唯一权威实现
- [ ] 报告逐 token 显示（curl -N 可见 message.delta 连续增量，T2-1/T2-2 通过）
- [ ] 人为注入节点异常（测试桩），收到 run.error 且流关闭不挂起（T2-3/T2-4 通过）
- [ ] 3 并发流不串流（T2-6 通过）
- [ ] 全部 SSE 事件过协议合规校验（T2-5 通过）
- [ ] 打 tag `p2-done`

## 7. 风险与对策（含 P2-0 结论回填区）

| 风险 | 对策 | 实测结论（回填） |
|------|------|------------------|
| qwen 流式在 messages-tuple 分块异常 | 第一天先跑 P2-0 最小验证，再铺开 | _待回填_ |
| X-Accel-Buffering/代理缓冲导致 SSE 聚包 | 已加响应头；开发环境直连 FastAPI 端口验证 | _待回填_ |
| astream 多 stream_mode 的 chunk 形态与预期不符 | 以 `langgraph` 仓库 `libs/` 源码为准核对；必要时单独订阅验证 | _待回填_ |
| 旧前端在 P2-P5 期间不可用 | 以 curl/SSE 调试脚本为准做验收（计划第九节策略：后端先稳定再动前端） | — |
