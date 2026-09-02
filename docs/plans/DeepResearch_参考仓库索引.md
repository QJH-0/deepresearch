# DeepResearch 重构 · 参考仓库索引

> 版本：v1.0 · 2026-09-02
> 目的：**重构期间不再到网络查找**。本文给出每个参考仓库的本地路径、锁定版本、用途、以及**关键文件入口的具体路径与行号锚点**，需要时直接打开对应文件即可。
> 配套文档：《DeepResearch_重构功能确认清单.md》（v1.2 决策）、《DeepResearch_重构详细计划.md》（v1.2 执行）
> 根目录：`D:\Code\LLMdev\deepresearch\参考项目\`

---

## 一、总表

| 仓库 | 本地路径 | 锁定 commit | 上游地址 | 主要用途 | 对应 Phase |
|------|----------|-------------|----------|----------|------------|
| `open_deep_research` | `参考项目/open_deep_research` | `1b7d2e8` | https://github.com/langchain-ai/open_deep_research | **后端编排首选**：HITL 澄清、Command resume、动态模型 | P1–P4 |
| `ai-chatbot` | `参考项目/ai-chatbot` | `c2f8235` | https://github.com/vercel/ai-chatbot | **SSE 事件协议**、流式状态机 | P0 / P2 / P6 |
| `open-agent-platform` | `参考项目/open-agent-platform` | `6a2ea0a` | https://github.com/langchain-ai/open-agent-platform | 前后端围绕 LangGraph 协作、HITL 审批 UI | P3 / P4 |
| `memory-template` | `参考项目/memory-template` | `c4e05d4` | https://github.com/langchain-ai/memory-template | langmem 热路径 + 后台记忆 | P5 |
| `gpt-researcher` | `参考项目/gpt-researcher` | `6f99857` | https://github.com/assafelovic/gpt-researcher | 报告引用体系 `[n]` 角标 | P7 |
| `lobe-chat` | `参考项目/lobe-chat` | `a45b700a` | https://github.com/lobehub/lobe-chat | 前端交互形态、消息渲染、会话管理 | P6 |
| `langgraph` | `参考项目/langgraph` | `11ee185` | https://github.com/langchain-ai/langgraph | 官方示例与源码（persistence / HITL / 流式） | P1–P5 |
| `open-canvas` | `参考项目/open-canvas` | `0310cec` | https://github.com/langchain-ai/open-canvas | 备用：多 Agent 前端交互 | P6（备选） |
| `langfuse` | `参考项目/langfuse` | `983c2a6` | https://github.com/langfuse/langfuse | 后续可选：可观测性（**本次不用**） | — |

**不再拉取**：~~`searxng`~~ —— web_search 已改接 DuckDuckGo（见第三节说明）。

补拉命令（缺失时执行）：

```powershell
cd "D:\Code\LLMdev\学习文档\参考项目"
git clone --depth 1 https://github.com/langchain-ai/open_deep_research.git
git clone --depth 1 https://github.com/vercel/ai-chatbot.git ai-chatbot
git clone --depth 1 https://github.com/langchain-ai/open-agent-platform.git
git clone --depth 1 https://github.com/langchain-ai/memory-template.git
git clone --depth 1 https://github.com/assafelovic/gpt-researcher.git
git clone --depth 1 https://github.com/lobehub/lobe-chat.git
git clone --depth 1 https://github.com/langchain-ai/langgraph.git
git clone --depth 1 https://github.com/langchain-ai/open-canvas.git
git clone --depth 1 https://github.com/langfuse/langfuse.git
```

---

## 二、逐仓库关键入口

### 2.1 `open_deep_research` —— 后端编排首选参考

| 看什么 | 文件:行 | 说明 |
|--------|---------|------|
| **HITL 澄清节点（D1/D2 模板）** | `src/open_deep_research/deep_researcher.py:60` | `async def clarify_with_user(...)`，用 `interrupt()` 提问，返回 `Command[Literal["write_research_brief","__end__"]]` |
| 澄清 prompt | `src/open_deep_research/prompts.py:3` | `clarify_with_user_instructions` |
| 澄清结构化输出模型 | `src/open_deep_research/state.py:30` | `class ClarifyWithUser(BaseModel)` |
| **动态模型配置（G5 直接抄）** | `src/open_deep_research/deep_researcher.py:56` | `configurable_model = init_chat_model(configurable_fields=("model","max_tokens","api_key"))` |
| 节点级模型绑定 | `src/open_deep_research/deep_researcher.py:81-93` | `model_config` → `.with_structured_output().with_retry().with_config()` |
| 图接线 | `src/open_deep_research/deep_researcher.py:708-714` | `add_node("clarify_with_user", …)` / `add_edge(START, "clarify_with_user")` |
| **State 分组 + reducer（第四节设计源）** | `src/open_deep_research/state.py:62-96` | `AgentInputState` / `AgentState` / `SupervisorState` / `ResearcherState`，全部用 `Annotated[..., override_reducer \| operator.add]` |
| 配置中心（pydantic） | `src/open_deep_research/configuration.py`（251 行） | `Configuration.from_runnable_config(config)` 模式，J3 配置统一可参照 |
| 旧版（supervisor 多 Agent） | `src/legacy/multi_agent.py`（487 行） | 另一套 supervisor 委派实现，仅作对照，**不要混用** |
| 旧版 interrupt | `src/legacy/graph.py:175` | `feedback = interrupt(interrupt_message)` |

> ⚠️ 注意 `src/legacy/` 是旧实现，与 `src/open_deep_research/` 并存。抄代码时**只抄 `src/open_deep_research/`**。

### 2.2 `ai-chatbot`（vercel）—— SSE 事件协议参考

| 看什么 | 文件:行 | 说明 |
|--------|---------|------|
| **流式主入口（P2 直接对照）** | `app/(chat)/api/chat/route.ts:205`、`:269`、`:332`、`:405` | 标准四段式：`createUIMessageStream({...})` → `streamText({...})` → `toUIMessageStream({...})` → `return createUIMessageStreamResponse({...})`。含错误兜底与持久化，是本计划 5.1 服务层 async generator 的对应物 |
| 历史流重放 | `app/(chat)/api/chat/[id]/stream/route.ts` | 断线重连时回放既有流，对应本计划 5.7 的"SSE 重连" |
| **事件驱动刷新（A3/A1 直接抄）** | `components/chat/data-stream-handler.tsx:26-29` | 收到 `data-chat-title` 事件 → `mutate()` 局部刷新会话列表。**这正是"事件驱动刷新替代 10 秒轮询"的现成实现** |
| 数据流上下文 | `components/chat/data-stream-provider.tsx` | 数据流在组件树中的分发方式 |
| 消息渲染 / 思考折叠 | `components/chat/message.tsx`、`components/chat/message-reasoning.tsx`、`components/chat/messages.tsx` | `message-reasoning.tsx` 即 B4 thinking 折叠的参考 |
| 前端消费封装 | `hooks/use-active-chat.tsx` | `useChat` 的封装位置，对应本计划 `useEventStream` composable |
| 配置 | `lib/`、`next.config.ts`、`proxy.ts` | 路由代理与模型配置组织方式 |

### 2.3 `open-agent-platform` —— 前后端围绕 LangGraph 协作

| 看什么 | 路径 | 说明 |
|--------|------|------|
| **HITL 审批 UI（P4-3 直接对照）** | `apps/web/src/components/agent-inbox/` | `inbox-view.tsx`、`thread-view.tsx`、`types.ts`、`hooks/`、`utils/` —— interrupt 列表 → 审批卡片 → resume 的完整前端闭环 |
| 对话区实现 | `apps/web/src/features/chat/` | 与 LangGraph Server 的交互层 |
| Agent/RAG 模块 | `apps/web/src/features/agents/`、`apps/web/src/features/rag/` | 无代码改 prompt 的产品化形态 |
| 概念说明 | `CONCEPTS.md`、`AGENTS.md` | 架构约定，先读这个再读代码 |

### 2.4 `memory-template` —— langmem 双通道记忆（P5 唯一参考）

| 看什么 | 文件:行 | 说明 |
|--------|---------|------|
| **后台记忆提取（langmem）** | `src/memory_graph/graph.py:13`、`:48` | `from langmem import create_memory_store_manager`，`create_memory_store_manager(...)` 完整参数范例 |
| **热路径记忆检索** | `src/chatbot/graph.py:31-36` | `namespace = (configurable.user_id,)` → `store.asearch(namespace, query=query, limit=10)`，直接对应本计划"plan 节点前注入记忆" |
| 远端 client | `src/chatbot/graph.py:11`、`:53` | `from langgraph_sdk import get_client` |
| 配置 | `src/chatbot/configuration.py`、`src/memory_graph/configuration.py` | 记忆相关配置项 |

> 只有 9 个源文件、30 个受版本控制文件，**半天能读完**，是 P5 的绝对主力参考。

### 2.5 `gpt-researcher` —— 报告引用体系（P7）

| 看什么 | 路径 | 说明 |
|--------|------|------|
| 报告生成 | `gpt_researcher/actions/report_generation.py` | 报告结构与 prompt 组织 |
| Markdown 处理 / 引用格式化 | `gpt_researcher/actions/markdown_processing.py` | `[n]` 角标与参考列表的落地处理 |
| 检索与上下文压缩 | `gpt_researcher/context/retriever.py`、`gpt_researcher/context/compression.py` | 来源去重与上下文裁剪 |
| 网页抓取（DuckDuckGo 接入可参照其 scraper 抽象） | `gpt_researcher/actions/web_scraping.py`、`gpt_researcher/skills/` | 抓取与结果结构化 |
| 中文文档 | `README-zh_CN.md` | 快速上手 |

### 2.6 `lobe-chat` —— 前端交互形态（P6）

| 看什么 | 路径 | 说明 |
|--------|------|------|
| **消息渲染** | `src/features/Conversation/ChatItem/`、`ChatItem/components/MessageContent` | 单条消息渲染、思考块折叠、错误态 |
| 会话列表 | `src/features/Conversation/ChatList/`、`ConversationProvider.tsx` | 会话切换与状态隔离（解决"切会话丢状态"） |
| **状态管理（Pinia 对应物）** | `src/store/chat/`（`store.ts`、`initialState.ts`、`slices/`、`selectors.ts`） | zustand slice 模式 → 映射到 Pinia store 划分 |
| 会话 store | `src/store/session/` | 会话列表与切换，对应 `stores/threads.ts` |
| **记忆面板 store（E3 直接抄）** | `src/store/userMemory/`（`store.ts`、`initialState.ts`、`selectors.ts`、`slices/`、`types.ts`） | 用户记忆的展示与管理，正是本计划 E3"记忆可见性"的产品化实现 |
| 其他 store | `src/store/`（`agent/`、`file/`、`tool/` …） | 多 store 边界划分，供本计划 2.3 节目录结构参考 |

### 2.7 `langgraph` —— 官方源码与示例

| 看什么 | 路径 | 说明 |
|--------|------|------|
| **HITL 示例（P4 用）** | `examples/human_in_the_loop/` | interrupt / resume 最小可运行范例 |
| 多 Agent 组织 | `examples/multi_agent/`、`examples/plan-and-execute/`、`examples/reflection/` | 对应本计划 G1 拓扑与 reflect 环节 |
| RAG / 子图 | `examples/rag/`、`examples/subgraph.ipynb` | 子图拆分与检索链路 |
| 源码（API 行为存疑时查这里） | `libs/` | checkpointer / Store / interrupt 的真实实现 |
| 文档入口 | `docs/llms.txt` | ⚠️ 该仓库 `docs/` 下只有 `llms.txt`（正式文档在独立仓库），需要时以 `llms.txt` 为索引 |

> 该仓库 `examples/` 下**没有 `persistence/` 目录**，持久化相关优先看 `libs/langgraph/checkpoint/` 源码与 `examples/human_in_the_loop/`。

### 2.8 `open-canvas` / `langfuse`（备用）

- `open-canvas`：多 Agent 前端交互的备选形态，P6 设计卡壳时再看。
- `langfuse`：**本次重构不纳入**，本地已备，后续接入可观测性时直接用。

---

## 三、关于 searxng（已弃用，附原因备查）

**决策**：不再拉取 `searxng`，web_search 改用 `duckduckgo-search` Python 库（免费、无 API key、纯库调用、无需自托管服务）。

**弃用原因（实测结论，避免以后重复踩坑）**：

- searxng 上游 `utils/templates/` 下有 4 个文件名含 `:` 的路径：
  - `utils/templates/etc/httpd/sites-available/searxng.conf:socket`
  - `utils/templates/etc/nginx/default.apps-available/searxng.conf:socket`
  - `utils/templates/etc/uwsgi/apps-archlinux/searxng.ini:socket`
  - `utils/templates/etc/uwsgi/apps-available/searxng.ini:socket`
- Windows 文件名禁止 `:`。git（实测 2.55.0.windows.3）在**把 tree 写入 index 阶段**就对整棵树做路径校验，sparse-checkout 的排除规则在此之后才生效。
- 已实测无效的方案：`git reset --hard`、`git checkout -f HEAD`、`git read-tree -mu HEAD`、非 cone 模式 sparse（`/*` + `!/utils/templates/`）、cone 模式 sparse、`git clone --depth 1 --no-checkout` + cone sparse。
- 附带坑：若仓库开了 `extensions.worktreeConfig`，`.git/config.worktree` 会覆盖 `.git/config`，改 sparse 相关配置要用 `git config --worktree`。
- 另外：cone 模式 sparse-checkout 的模式行必须是**目录**（形如 `/searx/`），写成文件（如 `/manage`）会静默禁用 cone 匹配。

---

## 四、使用约定

1. **优先查本文件**：重构中需要参考实现时，先按"对应 Phase"定位仓库，再按文件:行打开，不再联网搜索。
2. **版本锁定**：表中 commit 为当前锁定版本。如需升级，更新本表 commit 并同步《DeepResearch_重构详细计划.md》第一节。
3. **新增参考仓库**：先补本文件总表 + 关键入口，再在计划文档第一节同步一行。
4. **引用要落到行**：写方案或代码注释时引用参考实现，格式统一为 `仓库:文件路径:行号`，便于回溯核对。
