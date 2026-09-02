# Phase 6 · 前端重构 · 开发文档

> 依据：《DeepResearch_重构详细计划.md》v1.2 第五节 5.7、第二节 2.3、第六节 Phase 6；功能确认清单决策 1（Vue3+Pinia+UI 库）、14（A3/A4）
> 工期：4～5 天（最大块）｜前置依赖：Phase 0（事件协议冻结后可按 mock 先行开发）；完整联调依赖 P2-P5 完成｜并行窗口见 README 第四节
> 参考仓库：`lobe-chat`（交互形态，commit `a45b700a`）、`ai-chatbot`（事件驱动刷新）、`open-agent-platform`（HITL 审批 UI）
> **路径勘误**：前端实际路径 `agent_front/`（非计划文档所写 `front/agent_front/`）

---

## 1. 目标与范围

**结论先行**：前端整体重构为 **Vue3 + Pinia + Naive UI**，核心是 `useEventStream` 统一事件 reducer（run/resume 共用，消灭 ChatView.vue:134-198 与 227-263 的 70 行重复），四个 store（threads/chat/interrupt/documents），消息级 thinking 与来源，三张 HITL 卡片，ChatView 瘦身到 <150 行。会话标题自动生成（A3）与回滚入口（A4）一并落地。

**范围边界**：
- ✅ 做：P6-1～P6-7 全部任务（含 A3 标题生成后端部分、A4 回滚 UI 入口）
- ❌ 不做：引用角标渲染与 SourceList 深度交互（P7）、记忆面板（P5 的 /memories 数据源已就绪，本 Phase 只留侧栏占位）

## 2. 现状锚点（已核实，实际文件清单）

| 问题 | 位置（实际路径） | 说明 |
|------|------|------|
| ChatView 巨组件 | `agent_front/src/views/ChatView.vue`（479 行） | 目标 <150 行，只做布局组装 |
| 无 Pinia | `agent_front/src/stores/session.ts`（209 行，模块级单例） | 切会话丢状态 |
| 两段重复事件处理 | `ChatView.vue:134-198`（run）与 `:227-263`（resume） | 约 70 行重复逻辑 |
| 正则路由 resume | `ChatView.vue` / session.ts:38 | 匹配"继续"关键词，P4 结构化 payload 后删除 |
| 全局唯一 thinkingBlock | `agent_front/src/components/chat/ThinkingBlock.vue`（220 行）+ ChatView.vue:107 | 切会话即丢；改消息级 |
| interrupt 状态切会话清空 | session.ts:38 | interrupt store 持久化（D4）解决 |
| 手写 Markdown 渲染 | 无渲染库（MessageBubble.vue 仅 25 行壳） | 换 markdown-it + highlight.js + katex |
| 10s 轮询会话列表 | session.ts | 事件驱动刷新替代 |
| 自写 SSE 解析 | `agent_front/src/api/index.ts`（205 行） | EventSource/fetch-stream 封装 + 自动重连 |

**保留改造**：`components/knowledge/`（DocumentTable/StatsCards/UploadDropzone/UploadTaskList，接 documents store）、`views/KnowledgeView.vue`、路由骨架。

## 3. 目标目录结构

```
agent_front/src/
  stores/
    threads.ts        # 会话列表（事件驱动刷新，去 10s 轮询）
    chat.ts           # 当前会话消息流（消息级 thinking/来源，切会话不丢：Map<thread_id, Message[]>）
    interrupt.ts      # 审批/澄清状态（持久化：切会话回来还在，数据源 GET /threads/{id}/interrupt）
    documents.ts      # 文档库（原 session.ts 中文档部分拆出）
  api/
    sse.ts            # fetch 流式消费 + 断线自动重连（POST body 需 fetch，不能用 EventSource）
    rest.ts           # REST 封装（run/cancel/resume/threads/documents/memories）
  composables/
    useEventStream.ts # ★ 核心：统一事件 reducer，run/resume 共用
  components/chat/
    MessageList.vue  MessageItem.vue  MarkdownRender.vue
    ThinkingBlock.vue（消息级，重构）  AgentTimeline.vue
    PlanApprovalCard.vue  ClarifyCard.vue  ReportReviewCard.vue
    SourceList.vue（P7 完善，先占位）  StopButton.vue
  components/layout/
    AppSidebar.vue（重构）  ThreadHistory.vue（重构，接 threads store）
  views/ChatView.vue  # <150 行，只做布局与组装
```

## 4. 任务分解

### P6-1 脚手架（0.5 天）

```powershell
cd D:\Code\LLMdev\deepresearch\agent_front
npm i pinia naive-ui markdown-it highlight.js markdown-it-katex
# TypeScript 类型：event-protocol.json → src/types/events.gen.ts（脚本生成，见下）
```

**TS 类型生成脚本**（`scripts/gen-event-types.ts`，消费 P0 的 `docs/event-protocol.json`）：

```typescript
// 读 ../docs/event-protocol.json（10 个事件的 JSON Schema）
// 用 json-schema-to-typescript 生成 src/types/events.gen.ts
// 前后端事件字段从此单一信源，杜绝不一致（计划第三节落地形式）
```

### P6-2 useEventStream（核心，1 天）

```typescript
// composables/useEventStream.ts（骨架）
export function useEventStream() {
  const chat = useChatStore(); const intr = useInterruptStore();
  const threads = useThreadsStore();

  async function consume(threadId: string, resp: Response) {
    const reader = resp.body!.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      for (const line of takeSseLines(buf)) {          // 按 "data: {...}\n\n" 切帧
        const env = JSON.parse(line);                  // EventEnvelope
        switch (env.type) {
          case "message.delta":    chat.appendDelta(threadId, env.data); break;
          case "message.thinking": chat.appendThinking(threadId, env.data); break;
          case "agent.status":     chat.setNodeStatus(threadId, env.data); break;
          case "sources.found":    chat.addSources(threadId, env.data.sources); break;
          case "interrupt.raised": intr.raise(threadId, env.data); break;
          case "run.completed":    chat.finish(threadId, env.data); threads.refresh(threadId); break;
          case "run.cancelled":    chat.markCancelled(threadId); break;
          case "run.error":        chat.markError(threadId, env.data); break;
          default: break;                               // 不变式③：未知 type 静默忽略
        }
      }
    }
  }

  const run = (threadId: string, query: string) =>
    rest.postStream("/run", { thread_id: threadId, query }).then(r => consume(threadId, r));
  const resume = (threadId: string, payload: object) =>
    rest.postStream("/resume", { thread_id: threadId, ...payload }).then(r => consume(threadId, r));
  return { run, resume, consume };
}
```

**要点**：
1. **run 与 resume 共用 consume**——两入口只是请求不同，事件处理链唯一（消灭 ChatView 两段重复）。
2. 消息模型（chat store 内）：`{id, role, node, text, thinking: string, sources: Source[], status: streaming|done|error|cancelled}`——thinking 与 sources 挂在**消息上**，不是全局块。
3. SSE 帧解析健壮性：跨 chunk 的半行 JSON 缓冲处理（buf 残留拼接）。
4. **参考**：`ai-chatbot:components/chat/data-stream-handler.tsx:26-29`（收到 `data-chat-title` → `mutate()` 局部刷新会话列表——事件驱动刷新替代轮询的现成实现，A1/A3 直接抄模式）。

### P6-3 stores（1 天）

| store | 职责 | 关键设计 |
|-------|------|----------|
| threads.ts | 列表/重命名/置顶/删除 | 事件驱动刷新：useEventStream 的 run.completed/error 后 `refresh()`；标题生成事件后更新单条 |
| chat.ts | 消息流 | **`Map<thread_id, ThreadChat>` 结构，切会话不丢**（修复全局单例问题）；thinking/sources 消息级 |
| interrupt.ts | 审批/澄清状态 | 切入会话时若 store 无该 thread 状态 → 调 `GET /threads/{id}/interrupt` 重建（D4 持久化）；resume 成功后清除 |
| documents.ts | 文档库 | 原 knowledge 组件接此 store |

**参考**：`lobe-chat:src/store/chat/`（store.ts/initialState.ts/slices/selectors.ts 的 zustand slice 模式 → 映射 Pinia store 划分）、`src/store/session/`（会话列表）、`src/store/userMemory/`（记忆面板 store，E3 产品化实现，本 Phase 占位 P7 后接）。

### P6-4 组件（1.5 天）

| 组件 | 规格 | 参考 |
|------|------|------|
| MarkdownRender.vue | markdown-it + highlight.js + markdown-it-katex；代码块复制按钮；表格/公式渲染 | lobe-chat `src/features/Conversation/ChatItem/components/MessageContent` |
| ThinkingBlock.vue（重构） | 消息级、可折叠、流式追加动画 | lobe-chat message-reasoning 折叠；ai-chatbot `components/chat/message-reasoning.tsx` |
| AgentTimeline.vue | 消费 agent.status：节点进度时间线（已完成✓/进行中 spinner/待开始） | open-agent-platform thread-view |
| PlanApprovalCard.vue | 子问题列表 + 三按钮（批准/修改/否决）；"修改"弹**原因输入框**（NInput + 确认）；显示 revision_count（第 n/3 次修改） | open-agent-platform `agent-inbox/` 审批卡片 |
| ClarifyCard.vue | 澄清问题列表 + 多输入框回答 + 提交 | 同上 |
| ReportReviewCard.vue | 报告预览 + 采纳 / 再深入（方向输入，多选追加子问题） | 同上 |
| StopButton.vue | 生成中显示；点击调 /cancel；本地先置 cancelled 态（乐观更新） | lobe-chat 停止按钮形态 |
| SourceList.vue | 占位（P7 完善角标 tooltip + 侧栏） | — |

**HITL 卡片与后端契约**（P4 已冻结）：
- 数据源：`interrupt.raised` 事件（实时）+ `GET /threads/{id}/interrupt`（重连重建），两者 payload 结构一致。
- 提交：PlanApprovalCard → `resume({kind: "plan_approval", action, reason?})`；ClarifyCard → `resume({kind: "clarification", answers})`；ReportReviewCard → `resume({kind: "report_review", action, extra_sub_questions?})`。

### P6-5 ChatView 瘦身（0.5 天）

- 只做布局组装：`<AppSidebar/><ThreadHistory/><MessageList/><Composer/><SourceList/>` + useEventStream 挂载。
- 目标 <150 行；删除两段重复处理、正则路由、全局 thinking 引用。
- 删除旧组件：HelloWorld/TheWelcome/WelcomeItem/Icon* 脚手架残留。

### P6-6 会话标题自动生成（A3，0.5 天）

**后端**（本任务含后端小改动）：run.completed 后异步 LLM 生成标题（qwen-turbo，输入=首条用户问题+报告摘要，输出 ≤20 字标题）→ 更新 thread 元数据 → SSE 发 `chat.title` 类事件（或前端 run.completed 后直接 refresh threads 列表读取新标题）。
**前端**：threads 列表项展示标题；参考 `ai-chatbot:data-stream-handler.tsx:26-29` 的 mutate 模式与 lobe-chat 会话列表形态。

### P6-7 回滚入口（A4，0.5 天）

- 会话详情（ThreadHistory 下拉或会话顶部菜单）暴露 checkpoint 列表：调既有 `GET /history/{thread_id}`（`research_router.py:193`）展示检查点时间线。
- 选择某检查点 → 调既有 `POST /rollback`（`research_router.py:203`）→ 刷新消息流。
- 回滚后如 thread 处于 running，先 cancel 再回滚（409 防护联动）。

## 5. 测试计划

| 用例 | 类型 | 断言 |
|------|------|------|
| T6-1 事件 reducer 单测 | vitest | mock 事件序列（start→delta×3→thinking→status→completed）→ chat store 消息状态正确；未知 type 不抛错 |
| T6-2 半帧缓冲 | vitest | 人工切分 SSE 字节流（JSON 中间断开）→ 解析无损 |
| T6-3 run/resume 共链 | vitest | 两个入口产生的事件均被同一 reducer 处理；无第二套处理逻辑（代码断言：ChatView 无 switch(env.type)） |
| T6-4 切会话状态保留 | 手测 | A 会话生成中 → 切 B → 切回 A：消息流、thinking、interrupt 卡片完整还原 |
| T6-5 E2E 全流程 | 手测 | 上传文档 → 提问（research）→ 澄清回答 → 审批计划（修改一次+原因）→ 中途停止 → 恢复续跑 → token 流报告 → 引用侧栏（P7 前至少有数据） |
| T6-6 标题自动生成 | 手测 | 首轮研究完成后列表标题自动更新，无需手填 |
| T6-7 回滚 | 手测 | 选历史 checkpoint 回滚 → 消息流回到该点；running 状态下回滚先提示取消 |
| T6-8 无 console 报错 | 手测 | 全流程 DevTools Console 零 error |
| T6-9 SSE 断线重连 | 手测 | 流式中断网 3 秒恢复 → 按 GET /threads/{id}/state 结果决定"续传提示"或"静默重放" |

## 6. 验收清单

- [ ] Pinia + Naive UI 脚手架完成；events.gen.ts 从 event-protocol.json 生成（单一信源）
- [ ] useEventStream 统一 reducer，run/resume 共用（T6-3 代码断言通过）
- [ ] 四个 store 落地；切会话所有状态不丢（T6-4）
- [ ] 组件族完成：MarkdownRender（代码/表格/公式）、消息级 ThinkingBlock、AgentTimeline、三张 HITL 卡片（含 plan 修改原因输入框）、StopButton
- [ ] ChatView <150 行，无重复事件处理、无正则路由、无全局 thinking
- [ ] 会话列表事件驱动刷新，10s 轮询删除
- [ ] 首轮研究后标题自动生成（A3）
- [ ] 会话详情暴露 checkpoint 列表并可回滚（A4）
- [ ] 全流程 E2E 通过 + 无 console 报错（T6-5/T6-8）
- [ ] 打 tag `p6-done`

## 7. 风险与对策

| 风险 | 对策 |
|------|------|
| 体量大周期长，后端协议变动引发返工 | 并行纪律：P0-3 协议冻结后才动工；P2-P5 期间用 mock 事件流开发；联调期后端协议已稳定（计划第九节教训） |
| SSE 用 POST body 无法用原生 EventSource | api/sse.ts 用 fetch + ReadableStream 手写消费（骨架已定）；不引入 eventsource-polyfill |
| Naive UI 深色模式/主题定制工作量 | 默认亮色优先；主题变量集中在 App.vue，lobe-chat 风格仿形放后（P7 后打磨） |
| markdown-it-katex 维护停滞 | 备选 @vscode/markdown-it-katex 或 katex 直接渲染公式块 |
| interrupt 卡片双数据源（事件 vs API）payload 不一致 | P4 已约定 payload 契约；前端 TS 类型统一从 events.gen.ts 引用，联调期 T6-4 兜底 |
| 旧组件删除牵连 KnowledgeView | knowledge 组件族保留改造，只换 store 数据源；KnowledgeView 路由不动 |
