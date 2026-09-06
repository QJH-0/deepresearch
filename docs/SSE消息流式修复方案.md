# SSE 消息流式全链路诊断与修复方案

## 一、10 种消息状态清单

| # | 事件类型 | 后端发送方 | 前端处理 | 状态 |
|---|---------|-----------|---------|------|
| 1 | `run.started` | research_service.py L282 | useEventStream dispatch L67-71 | ✅ 正常 |
| 2 | `agent.status` | research_service.py L326/333/364 | dispatch L72-76 | ✅ 正常 |
| 3 | `message.start` | research_service.py L308 | dispatch L78-83 | ✅ 正常 |
| 4 | `message.delta` | research_service.py L309 | dispatch L85-92 | ⚠️ 有问题（见下） |
| 5 | `message.thinking` | research_service.py L319 | dispatch L94-98 | ✅ 正常 |
| 6 | `sources.found` | research_service.py L338 | dispatch L99-102 | ✅ 正常 |
| 7 | `interrupt.raised` | research_service.py L353 | dispatch L104-108 | ✅ 正常 |
| 8 | `run.completed` | research_service.py L390/405 | dispatch L109-113 | ⚠️ 有问题（见下） |
| 9 | `run.cancelled` | research_service.py L418 | dispatch L115-118 | ✅ 正常 |
| 10 | `run.error` | research_service.py L412/426 | dispatch L120-124 | ✅ 正常 |

## 二、问题诊断

### 问题 1：中间节点 delta 看不见 — `_invoke_json_agent` 用同步 invoke

**根因**：`_parsing.py` L61:

```python
result = agent.invoke({"messages": [human]})  # 同步调用，无 token 流式
```

intent、plan、analyze、reflect、deep_dive 等节点走 `_invoke_json_agent`，使用 `agent.invoke()`（同步），不走 `agent.astream()`。因此这些节点**无法产生 token 事件**，前端只能看到 `agent.status`（进度消息），看不到内容流式。

**影响**：用户在 intent → plan → web_search → local_rag → deep_dive → analyze 阶段只看到"正在..."的进度条，看不到实际推理内容。

**write 节点和 direct_answer 节点**已经用了 `agent.astream(stream_mode="messages")`，所以只有这两个节点能产出 token 流式。

### 问题 2：最终报告没有流式显示 — `run.completed` 的 final 覆盖逻辑

**根因**：chat store `finish()` 方法 L174-196:

```typescript
function finish(threadId, data) {
  if (t.streamingMessageId) {
    const msg = t.messages.find(m => m.id === t.streamingMessageId)
    if (msg) {
      msg.status = 'done'
      if (!msg.content && data.final) {
        msg.content = data.final  // 只在 content 为空时覆盖
      }
    }
  } else if (data.final) {
    // 没有 streaming 消息时，直接创建新消息
    t.messages.push({ content: data.final, status: 'done' })
  }
}
```

正常流程是：write 节点发 `message.start` → 多个 `message.delta`（流式拼接）→ `run.completed`。此时 `msg.content` 已有内容，`data.final` 不会覆盖——**这是正确的**。

但 `run.completed` 里的 `message_id` 是 `f"{run_id}:write"`，而 `message.delta` 里的 `message_id` 也是 `f"{run_id}:{node}"`——当 node 是 "write" 时，两者一致，流式正常。

**然而**：如果 write 节点的 `message.delta` 事件丢失了（例如 writer 注入失败），则 `msg.content` 为空，`data.final` 会一次性塞入——这就是用户看到的"最后的报告没有流式显示"。

### 问题 3：write 节点 token 流式可能丢失 — writer 注入时序

**根因**：`graph.py` L98:

```python
return workflow.compile(checkpointer=checkpointer)
```

LangGraph compile 默认**不注入 StreamWriter**到节点函数。节点函数签名有 `writer: StreamWriter = None`，但 LangGraph 需要在 compile 时设置 `stream_mode` 或使用 `StreamWriter` 的特殊注入机制。

**实际验证**：`research_service.py` L287-289 使用 `stream_mode=["custom", "updates"]`。当 LangGraph 以 `stream_mode="custom"` 调用时，`StreamWriter` 会通过**函数签名注入**的方式传给节点。但关键前提是节点函数参数名必须为 `writer`。

查看 `bind_agent` 函数 L136-142:

```python
def bind_agent(node_func, agent, agent_name):
    return partial(node_func, agent=agent, agent_name=agent_name)
```

这里用 `partial` 绑定了 `agent` 和 `agent_name`，但没有绑定 `writer`。LangGraph 的 StreamWriter 注入依赖参数名 `writer`，但 `partial` 已经消费了部分参数，LangGraph 检查剩余参数签名时应该能看到 `writer`。

**但**：`_invoke_json_agent` 是普通函数（非 async），内部调用 `agent.invoke()`。即使 writer 被正确注入，JSON 节点也不产生 token 流式（用同步 invoke 而非 astream）。只有 write 节点和 direct_answer 节点自己写了 astream 循环。

### 问题 4：前端 SSE 解析只认 `data:` 行，忽略 `event:` 行

`sse.ts` L40-43:

```typescript
const line = frame.trim()
if (!line.startsWith('data:')) continue
const payload = line.slice(5).trim()
```

后端只发 `data:` 行（`events.py` L126: `f"data: {json}\n\n"`），不发 `event:` 行，所以这里**没问题**。

### 问题 5：MarkdownRender 每次内容更新都重新渲染整个 Markdown

`MarkdownRender.vue` L64-68:

```typescript
const html = computed(() => {
  const processed = preProcessContent(props.content || '')
  const rendered = md.render(processed)
  return postProcessHtml(rendered)
})
```

每次 `message.delta` 追加 text → `msg.content` 变化 → computed 重新计算 → `md.render()` 重新解析整个内容。对于长报告，**每次 delta 都触发全量 markdown 重渲染**，导致：
- 性能差（长文本每次重新解析）
- 光标/滚动跳动
- 可能在中间 delta 时显示不完整的 markdown（如未闭合的代码块）

**这不是 bug 但影响用户体验**，需要优化为节流渲染。

## 三、修复方案

### 修复 A：让 `_invoke_json_agent` 也支持 astream token 流式

将 `_parsing.py` 改为 async，内部用 `agent.astream()` 替代 `agent.invoke()`，在流式过程中发送 token 事件：

**改动文件**：
- `app/mult_agents/nodes/_parsing.py` — 重写为 async + astream
- `app/mult_agents/nodes/intent.py` — intent_node 改为 async
- `app/mult_agents/nodes/plan.py` — plan_node 改为 async  
- `app/mult_agents/nodes/analyze.py` — analyze_node 改为 async
- `app/mult_agents/nodes/reflect_node` — reflect_node 改为 async
- `app/mult_agents/nodes/deep_dive.py` — deep_dive_node 改为 async
- `app/mult_agents/nodes/web_search.py` — 检查是否需要
- `app/mult_agents/nodes/local_rag.py` — 检查是否需要

### 修复 B：确保 run.completed 的 final 不覆盖已有流式内容

chat store `finish()` 方法已有 `if (!msg.content && data.final)` 保护，逻辑正确。但需要额外保证：当 streaming 消息存在时，不创建新消息。

### 修复 C：MarkdownRender 节流渲染

在 MarkdownRender.vue 中加入 `requestAnimationFrame` 或 debounce，避免每次 delta 都全量重渲染。在 streaming 状态下用纯文本显示，完成后再 markdown 渲染。

### 修复 D：验证 writer 注入链路

确保 graph.compile() 不需要额外参数即可让 StreamWriter 注入工作。LangGraph 的 StreamWriter 注入是通过函数签名自动检测的，只要 stream_mode 包含 "custom" 就会注入。

## 四、优先级

1. **P0 - 修复 A**：让所有节点都能产生 token 流式（核心问题）
2. **P1 - 修复 C**：MarkdownRender 节流（用户体验）
3. **P2 - 修复 B/D**：验证确认（防御性）
