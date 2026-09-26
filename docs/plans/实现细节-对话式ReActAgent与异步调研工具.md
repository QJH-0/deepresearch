# 实现细节：对话式 ReAct Agent + 异步调研工具

> 配套方案：`docs/plans/设计方案-对话式ReActAgent与异步调研工具.md`
> 本文是可直接照着写代码的级别：目录结构、State 定义、节点实现、工具契约、事件协议、API、测试清单。
> 日期：2026-09-22 ｜ 状态：待评审

---

## 0. 目录结构（新增 / 改动）

```
app/
├─ mult_agents/
│  └─ chat/                          【新增】对话层，与研究层平级
│     ├─ __init__.py
│     ├─ state.py                    ChatState
│     ├─ graph.py                    build_chat_app()
│     ├─ nodes.py                    prepare_turn / llm / finalize
│     ├─ tools.py                    deep_research 等 5 个新工具 + 复用既有 3 个
│     └─ prompts.py                  system prompt 与通知渲染模板
├─ backend/
│  ├─ router/
│  │  ├─ chat_router.py              【新增】
│  │  └─ research_router.py          【改动】新增 /research/runs/*
│  ├─ service/
│  │  ├─ chat_service.py             【新增】驱动 ChatGraph
│  │  ├─ research_job_service.py     【新增】后台任务生命周期
│  │  ├─ notify_queue.py             【新增】per-thread 通知流
│  │  ├─ event_bus.py                【新增】事件账本 + 实时扇出
│  │  ├─ artifact_store.py           【新增】报告落盘与分片读取
│  │  └─ task_registry.py            【改动】按 run_id 注册 + shield 退出
│  └─ schemas/
│     └─ events.py                   【改动】channel/seq + 新增事件类型
└─ test/
   ├─ test_chat_graph.py             【新增】
   ├─ test_research_job_service.py   【新增】
   ├─ test_notify_queue.py           【新增】
   ├─ test_tool_contracts.py         【新增】
   ├─ test_chat_cancel_race.py       【新增】
   └─ test_event_protocol_contract.py【改动】基准更新
```

**原则**：`app/mult_agents/` 下既有研究图**一行不改**。新增的 `chat/` 与既有 `nodes/` 平级，避免命名空间冲突。

---

## 1. ChatState

```python
# app/mult_agents/chat/state.py
"""对话层状态。

reducer 纪律沿用 app/mult_agents/state.py:1-14：
- messages 是累积型（add_messages），节点只返回本轮增量
- notify_cursor 是**当前值**，后写覆盖 —— 通知 payload 不进 state，
  否则累积列表只增不减，且 cancel/重放时会重复注入
"""
from typing import Annotated
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class ChatState(TypedDict):
    # 对话（累积）
    messages: Annotated[list[BaseMessage], add_messages]
    # 身份
    thread_id: str
    user_id: str
    tenant_id: str
    # 压缩后的历史摘要（当前值）
    conversation_summary: str
    # 已消费到的通知 seq（当前值）—— 幂等注入的关键
    notify_cursor: int
    # 活跃调研任务快照（当前值）：run_id / status / query / phase
    active_jobs: list[dict]
    # 本轮标识，便于日志与事件串联
    turn_id: str


def create_initial_chat_state(thread_id, user_id, tenant_id) -> ChatState:
    return {
        "messages": [],
        "thread_id": thread_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "conversation_summary": "",
        "notify_cursor": 0,
        "active_jobs": [],
        "turn_id": "",
    }
```

**为什么 `active_jobs` 是当前值而不是累积**：任务状态会变（running → completed），当前值语义让 `prepare_turn` 每次整体刷新即可，不产生"旧值 + 旧值"翻倍（本仓已踩过此坑）。

---

## 2. ChatGraph

```python
# app/mult_agents/chat/graph.py
"""对话图：prepare_turn → llm ⇄ tools → finalize。

不用 langgraph.prebuilt.create_react_agent：
需要 prepare_turn 注入后台通知、finalize 做压缩，自建 StateGraph 更可控，
且能绕开结构化输出策略（ToolStrategy / ProviderStrategy）的选型负担。
"""
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.base import BaseCheckpointSaver

from .state import ChatState
from .nodes import prepare_turn_node, llm_node, finalize_node
from .tools import build_chat_tools
from ..models import build_agent


def route_after_llm(state: ChatState) -> str:
    last = state["messages"][-1]
    return "tools" if getattr(last, "tool_calls", None) else "finalize"


def build_chat_app(llm, tools, checkpointer: BaseCheckpointSaver):
    workflow = StateGraph(ChatState)
    workflow.add_node("prepare_turn", prepare_turn_node)
    workflow.add_node("llm", lambda s, cfg: llm_node(s, llm, tools, cfg))
    workflow.add_node("tools", ToolNode(tools))
    workflow.add_node("finalize", finalize_node)

    workflow.add_edge(START, "prepare_turn")
    workflow.add_edge("prepare_turn", "llm")
    workflow.add_conditional_edges("llm", route_after_llm, ["tools", "finalize"])
    workflow.add_edge("tools", "llm")
    workflow.add_edge("finalize", END)
    return workflow.compile(checkpointer=checkpointer)
```

**递归上限**：ReAct 每轮工具调用约 2 个 super-step，设 `recursion_limit=50`（约 20 次工具调用）。不要复用 `runtime.recursion_limit_for()` —— 那是按研究图的循环上限推算的，量级不同。

**模型分档**：`config.json` 的 `node_models` 增一项，不硬编码：

```json
"node_models": {
  "chat_agent": { "model": "qwen3.8-max" },
  ...
}
```

---

## 3. prepare_turn —— 后台通知回流点

```python
# app/mult_agents/chat/nodes.py
async def prepare_turn_node(state: ChatState, config: RunnableConfig):
    """拉通知 → 注入 → 推进 cursor → 刷新 active_jobs。

    幂等性由 (notify_cursor, seq) 保证：
    重复进入本节点（重放 / 重试）只会拉到 seq > cursor 的通知。
    """
    cfg = config.get("configurable", {})
    thread_id = cfg.get("chat_thread_id") or state["thread_id"]
    cursor = state.get("notify_cursor", 0)

    notifications = await notify_queue.drain(thread_id, after=cursor)
    if not notifications and not state.get("active_jobs"):
        return {}

    patch: dict = {}
    if notifications:
        # 已按 seq 升序；只进 SystemMessage（见 §5 的坑）
        patch["messages"] = [
            SystemMessage(content=render_notification(n)) for n in notifications
        ]
        patch["notify_cursor"] = max(n["seq"] for n in notifications)

    jobs = await research_job_service.list_jobs(thread_id)
    if jobs or state.get("active_jobs"):
        patch["active_jobs"] = jobs

    return patch
```

### render_notification 模板

```
[后台任务通知]
run_id: r_01j...
状态: completed        # completed | cancelled | failed | interrupted
标题: <query>
耗时: 312s   证据: 47 条   来源: 23 个
摘要: <1–2k token，由 aux LLM 压缩>
关键结论:
  1. ...
  2. ...
完整报告: artifact_id=art_01j... （用 open_report 分片读取，不要一次性读入）
```

**状态为 `interrupted` 时**额外附一行：`需要你审批：<审批项摘要>，请用 /research/runs/{rid}/resume 响应`，让 agent 能自然地催用户。

---

## 4. 工具实现

### 4.1 上下文注入方式

工具需要 `chat_thread_id` / `user_id`。用 `RunnableConfig` 参数，LangChain/LangGraph 会自动注入：

```python
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig

@tool("deep_research", args_schema=DeepResearchInput)
async def deep_research(
    query: str,
    focus: list[str] | None = None,
    mode: str = "background",
    max_iterations: int | None = None,
    timeout_seconds: int = 120,
    config: RunnableConfig = None,
) -> str:
    ...
```

> **注意**：`args_schema` 里**不要**包含 `config`，否则它会被当成模型可见参数。

### 4.2 deep_research

```python
"""发起一次深度调研。

【重要】本工具默认**后台执行**并立即返回 run_id 句柄，不等调研完成。
调研完成后系统会自动把结果通知注入下一轮对话，你无需轮询追问。
只有在用户明确表示「等我一下 / 现在就要」时才用 mode="foreground"。
轻量事实性问题请用 web_search，不要启动深度调研。
"""
async def deep_research(query, focus=None, mode="background",
                        max_iterations=None, timeout_seconds=120, config=None):
    cfg = config.get("configurable", {})
    job = await research_job_service.launch(
        chat_thread_id=cfg["chat_thread_id"],
        user_id=cfg["user_id"],
        tenant_id=cfg["tenant_id"],
        query=query,
        focus=focus or [],
        max_iterations=max_iterations,
        hitl_enabled=cfg.get("hitl_enabled", True),
    )
    if mode == "foreground":
        # 有上限的等待；超时不失败，转为后台并回句柄
        done = await research_job_service.wait(job.run_id, timeout_seconds)
        if done:
            return await _render_result(job.run_id)
        return json.dumps({
            "run_id": job.run_id, "mode": "background",
            "status": "running",
            "note": f"超过 {timeout_seconds}s 未结束，已转为后台，完成后会通知你",
        }, ensure_ascii=False)
    return json.dumps({
        "run_id": job.run_id, "mode": "background", "status": "running",
        "note": "调研已在后台开始，完成后我会把结果带回来；期间你可以继续提问。",
    }, ensure_ascii=False)
```

### 4.3 research_result —— 上下文护栏

```python
"""读取调研结果。返回**摘要**（≤ max_chars），不是报告全文。
需要原文时用 open_report 按 offset 分片读取，不要一次性把报告读进对话。"""
async def research_result(run_id: str, max_chars: int = 2000, config=None) -> str:
    job = await research_job_service.get(run_id)
    if job.status != "completed":
        return json.dumps({
            "status": job.status, "phase": job.phase,
            "hint": "尚未完成，可用 research_status 查进度，或稍等通知",
        }, ensure_ascii=False)

    report = await artifact_store.read(job.artifact_id)
    summary = job.summary or await condense_report(report, max_chars)  # aux LLM
    return json.dumps({
        "status": "completed",
        "summary": summary,
        "key_findings": job.key_findings[:5],
        "artifact_id": job.artifact_id,
        "source_count": job.source_count,
        "report_chars": len(report),
        "hint": "完整报告请用 open_report 分片读取",
    }, ensure_ascii=False)
```

**硬断言**：`len(returned) <= max_chars * 1.5`。这条写成测试（`test_tool_contracts.py`），防止有人图省事直接回 `final`。

### 4.4 其余工具

| 工具 | 要点 |
| --- | --- |
| `research_status` | 只返回 `{phase, progress_pct, last_events[3], elapsed_s}`，不返回报告 |
| `research_cancel` | `action: "interrupt"（默认，保 checkpoint）/ "rollback"`；返回 `{ok, checkpoint_kept}` |
| `open_report` | `offset_chars` + `max_chars`（默认 4000，上限 8000）；返回含 `has_more` |
| `web_search` / `fetch_url` / `kb_search` | 直接复用 `app/mult_agents/tools.py:431/631/397`，不重新实现 |

---

## 5. 坑：`ToolMessage` 跨轮次注入会报 400

后台任务完成时，直觉是补一条配对的 `ToolMessage(tool_call_id=...)`。但**多数 OpenAI 兼容实现要求 `ToolMessage` 必须紧跟在带 `tool_calls` 的 `AIMessage` 之后**；跨轮次注入时那条 AIMessage 早被滚动或压缩掉了，直接 400。

**决定**：`prepare_turn` 一律注入 **`SystemMessage`**。system 角色在中途插入是协议允许的，不要求配对。若某天确实需要 ToolMessage 语义，必须同时重放配对的 `AIMessage(tool_calls=[...])`——噪音大，不推荐。

---

## 6. ResearchJobService

```python
# app/backend/service/research_job_service.py
class ResearchJobService:
    """后台调研任务的生命周期：launch / wait / status / cancel / resume。

    与既有 ResearchService 的分工：
    - ResearchService  负责「怎么跑一张图」（astream、事件翻译）
    - ResearchJobService 负责「什么时候跑、跑不跑得动、跑完通知谁」
    """

    async def launch(self, *, chat_thread_id, user_id, tenant_id,
                     query, focus, max_iterations, hitl_enabled) -> Job:
        await self._quota.check(user_id)                 # 并发上限，默认 3
        run_id = new_id("r")
        research_thread_id = f"research:{chat_thread_id}:{run_id}"
        job = Job(run_id=run_id, chat_thread_id=chat_thread_id,
                  research_thread_id=research_thread_id, status="running")
        await self._jobs.upsert(job)

        task = asyncio.create_task(self._run(job), name=f"research-job:{run_id}")
        await self._registry.register(run_id, task)      # 注意：按 run_id 注册
        return job

    async def _run(self, job: Job) -> None:
        """任务协程。退出清理必须 shield（langgraph#5682）。"""
        try:
            await self._execute_graph(job)               # 内部 astream + 事件发布
        except asyncio.CancelledError:
            await self._on_cancelled(job)                # 保 checkpoint，落 job.cancelled
            raise
        except Exception as exc:
            await self._on_failed(job, exc)
        else:
            await self._on_completed(job)
        finally:
            await asyncio.shield(self._cleanup(job))     # ← 防 anyio 取消作用域吞掉清理

    async def _on_completed(self, job: Job) -> None:
        report = job.final_text
        artifact_id = await artifact_store.save(run_id=job.run_id, text=report,
                                                meta=job.source_index)
        summary = await condense_report(report, max_chars=2000)   # aux LLM
        job.update(status="completed", artifact_id=artifact_id, summary=summary)
        await self._jobs.upsert(job)
        await event_bus.publish(f"research:{job.run_id}", event("job.completed", ...))
        await notify_queue.push(job.chat_thread_id, {
            "kind": "job.completed", "run_id": job.run_id,
            "summary": summary, "artifact_id": artifact_id, ...
        })
```

### 6.1 TaskRegistry 改动点

现有 `TaskRegistry` 按 `thread_id` 注册，且 `register` 对同一 thread 并发会抛 `ConcurrentRunError`。新场景是「一个会话 thread 下多个 run 并存」，所以：

- `register` 的 key 改为 **`run_id`**（研究 thread 由 run_id 派生，一对一）
- 并发限制移到 `ResearchJobService._quota`（按 user 计，默认 3），不再按 thread
- 保留 Redis Pub/Sub 广播与 `scan_orphans`，channel 与 key 前缀沿用

---

## 7. NotifyQueue / EventBus / ArtifactStore

### 7.1 NotifyQueue（per-thread 通知流）

```python
class NotifyQueue:
    """per-thread 通知流：seq 单调、消费幂等。

    P0–P4 单机：asyncio.Queue + 进程内 dict（seq 用全局单调计数器）
    P5 多 worker：Redis Stream XADD / XRANGE，seq 用 stream id
    """
    async def push(self, chat_thread_id: str, payload: dict) -> int: ...
    async def drain(self, chat_thread_id: str, after: int) -> list[dict]:
        """返回 seq > after 的通知，按 seq 升序。不删除，只移动 cursor。"""
```

**不删除**：保留历史便于排查与重放，靠 cursor 推进实现"消费"。若要防止无限增长，给 Redis Stream 设 `MAXLEN ~ 1000`。

### 7.2 EventBus（三层解耦的第二层）

```python
class EventBus:
    async def publish(self, channel: str, envelope: EventEnvelope) -> int:
        """落账本（Redis Stream / PG）后扇出到实时订阅者。返回 seq。"""
    async def subscribe(self, channels: list[str],
                        last_id: str | int | None) -> AsyncIterator[tuple[str, int, EventEnvelope]]:
        """支持 Last-Event-ID 重放。"""
```

与 LangGraph 的 custom stream 是**互补**关系：custom stream 负责节点内 token 级实时流（不进 checkpoint，C5），EventBus 负责可重放的事实账本。两者都写，别合并。

### 7.3 ArtifactStore

```python
class ArtifactStore:
    async def save(self, run_id: str, text: str, meta: dict) -> str:
        """落盘（本地 output/reports/{run_id}.md，或 MinIO）+ 元数据。返回 artifact_id。"""
    async def read(self, artifact_id: str, offset_chars=0, max_chars=4000) -> str: ...
```

落盘后**复用既有门禁**：导出前 `report_check.check_report()`，PDF 导出后 `pdf_export_service.verify_pdf()` 回读。

---

## 8. 事件协议改动

```python
# app/backend/schemas/events.py
class EventEnvelope(BaseModel):
    type: str
    ts: int = Field(default_factory=lambda: int(time.time() * 1000))
    channel: str = "chat"      # "chat" | "research:{run_id}"
    seq: int = 0               # 单调递增，用于 Last-Event-ID 重放
    data: dict
```

新增 data 模型与注册：

```python
class JobStartedData(BaseModel):
    run_id: str; chat_thread_id: str; query: str

class JobProgressData(BaseModel):
    run_id: str; node: str; label: str; phase: str
    progress_pct: int = 0; detail: str = ""

class JobCompletedData(BaseModel):
    run_id: str; artifact_id: str; summary: str
    source_count: int = 0; elapsed_s: int = 0

class JobCancelledData(BaseModel):
    run_id: str; reason: str; checkpoint_kept: bool

class JobFailedData(BaseModel):
    run_id: str; code: str; message: str

class JobInterruptedData(BaseModel):
    run_id: str; interrupt_id: str; kind: str; payload: dict

# chat.* 侧
class ChatTurnStartedData(BaseModel): turn_id: str
class ChatDeltaData(BaseModel):       turn_id: str; text: str
class ChatToolCallData(BaseModel):    turn_id: str; name: str; args: dict
class ChatToolResultData(BaseModel):  turn_id: str; name: str; ok: bool; preview: str = ""
class ChatTurnCompletedData(BaseModel): turn_id: str; message_id: str
```

**三处必须同步**：`EVENT_REGISTRY` → `docs/event-protocol.json`（重新导出）→ `test_event_protocol_contract.py` 基准。改完后**注入一次人为漂移**确认测试真会失败。

---

## 9. API

### 9.1 新增

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/chat/threads` | 创建会话，返回 `chat_thread_id` |
| POST | `/chat/threads/{tid}/messages` | 发消息，SSE 返回（channel=chat） |
| GET | `/chat/threads/{tid}/stream` | **多路复用** SSE：chat + 该会话全部活跃 research 通道 |
| GET | `/chat/threads/{tid}/state` | 会话状态与 active_jobs |
| POST | `/research/runs` | 启动（agent 内部也走同一入口） |
| GET | `/research/runs/{rid}` | 状态 |
| GET | `/research/runs/{rid}/events` | SSE，支持 `Last-Event-ID` |
| POST | `/research/runs/{rid}/cancel` | body `{action: interrupt\|rollback}` |
| POST | `/research/runs/{rid}/resume` | body `{decision: approve\|revise\|reject, feedback}` |
| GET | `/research/runs/{rid}/report` | 报告全文（前端报告面板） |

### 9.2 保留

`/research/run`、`/research/stream`、`/research/cancel`、`/research/resume`、`/research/state`、`/research/export/*` **全部保留**，行为不变 —— 现有前端与测试不受影响。

### 9.3 多路复用 SSE 的帧格式

复用既有 `sse()` 输出，前端按 `channel` 分发：

```
data: {"type":"job.progress","channel":"research:r_01j","seq":42,"ts":...,"data":{...}}

data: {"type":"chat.delta","channel":"chat","seq":7,"ts":...,"data":{...}}
```

心跳沿用 `config.json` 的 `sse_heartbeat_seconds: 15`。

---

## 10. 前端改动

| 文件 | 改动 |
| --- | --- |
| `agent_front/src/composables/useEventStream.ts` | reducer 增加 `channel` 分发：chat 通道 → 消息流；research 通道 → 按 run_id 归档到 job store |
| 新增 `stores/jobs.ts` | `jobs: Record<run_id, {phase, progress, last_events}>` |
| 新增 `components/ResearchJobCard.vue` | 进度卡：阶段、进度条、最近 3 条事件、取消按钮 |
| 新增 `components/InterruptApprovalCard.vue` | 审批卡：approve / revise（带反馈）/ reject |
| 新增 `components/ReportPanel.vue` | 报告面板：调 `open_report` 语义分片懒加载 |
| `stores/threads.ts` | thread_id 生成改为 `chat-${Date.now()}`，派生研究 thread 由后端负责 |

**不变式**：前端对未知 `type` 静默忽略（既有协议不变式 3），所以后端可以先上线。

---

## 11. 测试清单

| 文件 | 用例要点 |
| --- | --- |
| `test_chat_graph.py` | ① `prepare_turn` 注入后 `notify_cursor` 单调推进；② 重复 drain 不重复注入（幂等）；③ foreground 超时转后台；④ 基线不破 |
| `test_research_job_service.py` | ① launch → status=running；② cancel(interrupt) 后 checkpoint 仍在（可 `aget_state` 读到 next）；③ cancel(rollback) 后 checkpoint 清除；④ 并发配额超限抛错 |
| `test_notify_queue.py` | ① 并发 push N 条，drain 恰好 N 条且 seq 有序；② `drain(after=cursor)` 幂等；③ 空队列返回 `{}` patch |
| `test_tool_contracts.py` | ① `research_result` 返回长度 ≤ `max_chars*1.5`（硬护栏）；② 未完成时返回 status 不抛错；③ `open_report` 分片不越界、有 `has_more` |
| `test_chat_cancel_race.py` | 取消进行中的任务，`shield` 清理仍然执行；取消后图不继续产 token |
| `test_event_protocol_contract.py` | 更新基准；注入人为漂移确认会失败 |

### 运行方式（沿用项目基线）

```bash
# 必须用 conda llmdev（managed 3.13 无 pytest）
D:/develop_tools/miniconda3/envs/llmdev/python.exe -m pytest app/test \
  --ignore=app/test/test_fix_regressions.py -q

# 回归专项（deselect 需 RabbitMQ 的三个类）
D:/develop_tools/miniconda3/envs/llmdev/python.exe -m pytest app/test/test_fix_regressions.py -q \
  --deselect app/test/test_fix_regressions.py::TestMqProducerDlqResilience \
  --deselect app/test/test_fix_regressions.py::TestPublishMessagesHonoursReturnValue \
  --deselect app/test/test_fix_regressions.py::TestRetryFailedChunksKeepsMessageIds
```

基线：**668 passed / 2 skipped** + 回归专项 **116 passed / 7 deselected**。低于此即视为回归。

---

## 12. 分阶段实施顺序（强依赖，不可跳步）

```
P0 ArtifactStore + 事件协议扩展 + 契约测试
      ↓
P1 ResearchJobService + /research/runs/* + TaskRegistry 改造
      ↓   （此时后端已能后台跑调研，前端未变，可用 curl 验证）
P2 ChatState / ChatGraph / 工具集 / ChatService + 单测
      ↓
P3 NotifyQueue + prepare_turn 注入 + 取消/中断通知
      ↓   （此时后端能力完整，可用脚本驱动多轮对话验证）
P4 前端多路复用 + 进度卡 + 审批卡 + 报告面板
      ↓
P5 Redis Stream + Last-Event-ID 重放 + orphan 扫描 + 断点续跑
      ↓
P6 四项评测纳入 CI 门禁
```

**P1 结束就有可见收益**：即使对话层还没做完，后台任务 API 已经能让现有前端改成"发起后轮询"，先解决阻塞问题。

---

## 13. 实施期纪律（本仓既有约定，别违反）

1. **切分支前**工作区必须干净；未提交改动先提交或 `git stash push -u -m`（`.ai-shared/AGENTS.md` 三道闸）。
2. **不用 `git rm`**（本机已知会连带删目录），删文件用 `rm` + `git add -A`。
3. 模型型号**不硬编码**，一律走 `config.json` 的 `node_models`。
4. 结构化输出：本次 ChatGraph 主循环**不结构化**；确实需要时显式传 `ProviderStrategy(schema, strict=True)`（无工具）或 `ToolStrategy`（带工具）。
5. 上下文注入用 `SystemMessage`，不用跨轮次 `ToolMessage`。
6. 每处 `except` 必须回答"异常去哪了"；取消路径的 `CancelledError` 必须 `raise` 回去，不要吞。
