# 计划：用户停止后输入"继续"→ 从 checkpoint 续流而非新任务

> 创建日期 2026-09-07
> 需求来源：用户反馈"手动停止后输入继续，应该续流之前中断的任务，而不是开始新任务"

## 目标

用户在同一会话窗口内手动停止研究后，再次输入消息发送时，前端应先检查后端是否存在可恢复的 checkpoint：
- **可恢复**（`resumable: true`）→ 调 `POST /resume {mode:"continue"}` 从断点续流
- **不可恢复** → 走 `POST /stream` 开始新任务（现有逻辑）

## 阻塞问题：0 个

后端 `POST /resume`、`GET /state/{thread_id}`、`resume_stream` 已全部实现。前端 `useEventStream` 已有 `scheduleReconnect` 中的续流逻辑可复用。只需在 `onSend` 入口加一层判断。

## 假设

1. 用户手动停止后，后端 `run.cancelled` 已发出，generator 已关闭，但 **checkpoint 仍在 PG 中**（`snapshot.next` 非空）—— 因为 cancel 是在 asyncio task 层面取消，checkpoint 已在取消前的超级步保存
2. `GET /state/{thread_id}` 返回 `status: "idle"`, `resumable: true`, `next_nodes: [...]` —— 验证点：cancel 后 checkpoint 的 next 是否为空
3. 用户输入的内容（如"继续"）在续流场景下应作为"继续信号"而非新问题——续流时 `mode=continue` 不传 query，后端 `astream(None, config)` 从 checkpoint 恢复，忽略用户输入的文本
4. 如果 checkpoint 已不可恢复（如任务已正常完成 `next` 为空），用户输入应走新任务

## 方案

### 修改文件

| 文件 | 修改内容 |
|---|---|
| `agent_front/src/views/ChatView.vue` | `onSend` 增加可恢复性检查分支 |
| `agent_front/src/composables/useEventStream.ts` | 新增 `runOrResume()` 方法，封装"先查 state 再决定走 run 还是 resume"逻辑 |

### 核心设计

在 `onSend` 中，当用户没有 HITL interrupt 卡片时，不直接调 `run()`，而是先调 `GET /state/{thread_id}` 检查：

```
onSend(text)
  ├── 有 HITL interrupt? → resume(mode=answer)     [现有逻辑不变]
  └── 无 interrupt
        ├── GET /state/{thread_id}
        │     ├── resumable: true → 消息对齐 + resume(mode=continue)  [新增]
        │     └── resumable: false → run(text)                       [现有逻辑]
```

### `runOrResume` 方法设计

```typescript
async function runOrResume(threadId: string, query: string, options?: {...}): Promise<void> {
  // 先检查是否有可恢复的 checkpoint
  try {
    const state = await fetchThreadState(threadId)
    if (state.resumable && state.status !== 'running') {
      // 可恢复 → 消息对齐 + resume 续流
      chat.ensureThread(threadId)
      chat.addUserMessage(threadId, query)    // 用户消息仍展示在界面上
      await syncThreadMessages(threadId)       // 以服务端为事实源对齐
      const resp = await postStream('/api/v1/research/resume', {
        thread_id: threadId,
        mode: 'continue',
      })
      await consume(threadId, resp)
      return
    }
  } catch {
    // 状态检查失败，降级走新 run
  }
  // 不可恢复或检查失败 → 新任务
  await run(threadId, query, options)
}
```

### 用户体验

- 用户输入"继续" → 界面显示用户消息"继续" → 后端从 checkpoint 续流 → SSE 继续推送剩余的 token/进度
- 如果任务已正常完成（`next` 为空），`resumable: false` → "继续"被当作新问题开始新研究

## 开发顺序

1. `useEventStream.ts`：新增 `runOrResume` 方法
2. `ChatView.vue`：`onSend` 中将 `run` 调用替换为 `runOrResume`
3. 验证 lint 无报错
