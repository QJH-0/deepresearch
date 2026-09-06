# 计划：用户停止后输入"继续"→ 从 checkpoint 续流（v2）

> 创建日期 2026-09-07
> 需求来源：用户反馈"手动停止后输入继续，应该续流之前中断的任务，而不是开始新任务"
> 版本：v2（解决 v1 的"新任务 vs 续流"冲突问题）

## 问题

v1 方案（`runOrResume`）的缺陷：如果用户停止后输入的是**全新问题**（如"帮我查天气"），`runOrResume` 检查到 `resumable: true` 就会无条件续流旧任务，完全忽略用户的新意图。

## 业界调研结论

### 1. Vercel AI SDK — `Chatbot Resume Streams`

- **核心模式**：`stop()` 只断开客户端连接，不取消后端工作
- **显式停止**：需要单独的 `/stop` 端点来持久化部分响应、取消后端工作、清除 active stream
- **续流机制**：页面刷新/导航离开后，通过 `resume` 选项自动重连到 active stream
- **关键设计**：区分"断开"和"停止"——断开可重连，停止不可重连

### 2. LangGraph — `Interrupts & Persistence`

- **核心模式**：`graph.invoke(None, config)` 是断点续跑的标准模式
- **checkpoint 语义**：`None` 输入 = 从最后 checkpoint 续跑，保留已检索的 sources/findings
- **thread_id 语义**：复用 `thread_id` 续跑同一 checkpoint，新 `thread_id` 开启新线程
- **HITL 模式**：`Command(resume=value)` 从 interrupt 点继续

### 3. ai-chatbot（Vercel 参考项目）

- `useAutoResume` hook：页面加载时如果最后一条消息是 user 角色，自动调 `resumeStream()`
- 通过检查消息角色判断是否需要续流，而非猜测用户意图

### 4. gpt-researcher

- 用 localStorage + 服务端双写做报告历史持久化
- **没有 checkpoint 续跑能力**——反面教材

## 方案设计

### 核心决策：区分"断开"和"停止"

| 场景 | 用户行为 | 前端动作 | 后端状态 | 再次输入时的行为 |
|---|---|---|---|---|
| **网络断开** | 网络波动、刷新页面 | 自动重连 `scheduleReconnect` | `resumable: true` | 自动续流（已有逻辑） |
| **用户手动停止** | 点停止按钮 | 调 `/cancel` + `markCancelled` | `resumable: true`（checkpoint 仍在） | **需要判断用户意图** |
| **HITL 中断** | 等待用户审批/澄清 | 显示 HITL 卡片 | `status: awaiting_input` | 走 `resume(mode=answer)`（已有逻辑） |

### 方案：前端状态标记 + 关键词匹配（推荐）

**不引入 LLM 意图分类**（避免延迟和成本），改为：

1. **用户手动停止时，前端标记 `userStopped: true`**
2. **用户再次输入时**：
   - 如果输入匹配"继续/续流/resume/continue/接着"等关键词 → 调 `POST /resume {mode:"continue"}`
   - 否则 → 走 `POST /stream` 新任务
3. **新任务启动时，清除 `userStopped` 标记**

**为什么不用 LLM 意图分类？**
- 增加 200-500ms 延迟（用户发送消息需要等 LLM 判断）
- 增加 API 成本（每次发送都要调一次 LLM）
- 用户明确说"继续"时，不需要 LLM 也能判断

**为什么不用交互式选择卡片？**
- 多一步交互，打断用户流畅体验
- 与现有 HITL 卡片模式冲突（HITL 是阻塞等待，这里是分支选择）

### 修改文件

| 文件 | 修改内容 |
|---|---|
| `agent_front/src/stores/chat.ts` | 新增 `userStopped` 状态字段，支持 `setUserStopped(threadId, bool)` |
| `agent_front/src/views/ChatView.vue` | `onStop` 中设置 `chat.setUserStopped(threadId, true)` |
| `agent_front/src/composables/useEventStream.ts` | `runOrResume` 改为：先检查 `userStopped`，再关键词匹配决定续流或新任务；`run` 启动时清除 `userStopped` |

### 关键词列表

```typescript
const RESUME_KEYWORDS = ['继续', '续流', 'resume', 'continue', '接着', '接着来', 'go on', 'proceed']
```

匹配规则：输入文本（去除首尾空格后）完全匹配或包含任一关键词。

### 时序图

```
用户点停止
  │
  ▼
ChatView.onStop()
  ├── cancelResearch(threadId)     → 后端取消
  └── chat.setUserStopped(threadId, true)
  │
用户输入"继续"
  │
  ▼
ChatView.onSend("继续")
  ├── intr.has(id)? → 否
  └── runOrResume(id, "继续")
        ├── chat.userStopped === true?
        │     └── 是 → 关键词匹配 "继续" ∈ RESUME_KEYWORDS?
        │           └── 是 → POST /resume {mode:"continue"} → 续流
        │           └── 否 → run("继续") → 新任务
        └── chat.userStopped !== true?
              └── run("继续") → 新任务（默认行为）
  │
用户输入"帮我查天气"
  │
  ▼
ChatView.onSend("帮我查天气")
  └── runOrResume(id, "帮我查天气")
        ├── chat.userStopped === true?
        │     └── 是 → 关键词匹配? → 否
        │           └── run("帮我查天气") → 新任务
        └── chat.userStopped !== true?
              └── run("帮我查天气") → 新任务
```

### 边界情况

| 场景 | 行为 |
|---|---|
| 用户停止后刷新页面 | `userStopped` 丢失（Pinia 非持久化），默认走新任务——可接受，因为刷新后用户通常想开始新对话 |
| 用户停止后切换会话再切回 | `userStopped` 按 threadId 存储，切换会话不影响 |
| 网络断开（非用户停止） | `userStopped` 为 false，`scheduleReconnect` 自动续流（已有逻辑） |
| 用户停止后输入"继续调研AI" | 包含"继续"关键词 → 续流。如果用户本意是新任务，需要更精确的关键词或改用 LLM 分类 |

## 开发顺序

1. `chat.ts` store：新增 `userStopped` 字段和操作方法
2. `ChatView.vue`：`onStop` 中设置 `userStopped = true`
3. `useEventStream.ts`：修改 `runOrResume` 实现关键词匹配逻辑；`run` 中清除 `userStopped`
4. 验证 lint 无报错

## 备选方案（若关键词匹配不够准确）

如果实际使用中发现关键词匹配误判率高，可升级为：

**方案 B：LLM 轻量意图分类**
- 后端新增 `/api/v1/research/intent` 端点
- 用轻量模型（如 qwen-turbo）做二分类："resume_previous_task" vs "start_new_task"
- 输入：用户新消息 + 当前 checkpoint 的 query
- 延迟约 200-500ms，成本极低

**方案 C：交互式选择卡片**
- 检测到 `resumable: true` 且 `userStopped: true` 时，弹出选择卡片
- "继续上次研究" / "开始新研究"
- 无误判，但多一步交互
