# DeepResearch 重构 · 功能确认清单

> 版本：v1.2（v1.1 基础上变更：web_search 由「自托管 SearXNG」改为 **DuckDuckGo（duckduckgo-search 库）**，searxng 不再作为参考仓库拉取）
> 目的：在产出详细重构计划之前，先逐项确认重构后的系统需要哪些功能、哪些保留、哪些重写、哪些删除。
> 关键决策已于 2026-09-02 确认（见第四节），详细执行见《DeepResearch_重构详细计划.md》v1.2。

---

## 一、为什么重构（现状诊断结论）

对 `deep_research` 代码的完整走读结论如下，这是重构范围的依据：

**架构本身没有问题，问题集中在工程实现层。**

整体分层（router / schemas / service / infra）清晰，多智能体拓扑（intent → plan → 双路检索 → deep_dive → analyze → reflect/write）设计合理，可以作为资产保留。

但以下功能实现存在硬伤（均已定位到代码）：

| # | 功能 | 问题 | 证据位置 |
|---|------|------|----------|
| 1 | 流式输出 | 非 token 级流式，custom 事件只推进度文案，最终报告一次性整块下发 | `app/backend/service/workflow_service.py:549-652`、`nodes.py:178,184` |
| 2 | 流式输出 | SSE finally 块引用未绑定变量，异常时抛 NameError 且 `__done__` 不再发出，**前端流永久挂起** | `workflow_service.py:641` |
| 3 | 中断/取消 | cancel_event 只在节点间隙检查，节点内 LLM 调用无法中断；按 thread_id 存 flag 无并发保护 | `workflow_service.py:278` |
| 4 | HITL | resume 无 final 时用全新 initial_state 重跑全图，可能覆盖已有进度 | `workflow_service.py:330` |
| 5 | 记忆 | 长期记忆用 MD5 哈希伪造 384 维向量，语义检索召回等于随机 | `app/mult_agents/memory/long_term.py:43-59` |
| 6 | 记忆 | MemoryManager 1434 行巨类，混合 Redis/PG/Milvus/摘要 LLM 多职责 | `memory/manager.py` |
| 7 | State | 41 字段扁平 TypedDict，仅 messages 有 reducer；节点写入 State 不存在的键被静默丢弃；引用不存在的 `state["hypotheses"]` | `nodes.py:1010,622,646`、`state.py:9-50` |
| 8 | 死代码 | `mult_agents/main.py` 保留一套旧的无 HITL 节点实现，与 nodes.py 同名冲突隐患 | `main.py:113-271` |
| 9 | 前端 | 无 Pinia（模块级单例）、无 UI 库、自写 SSE 解析与 Markdown 渲染；runResearch/resumeTask 两段约 70 行逻辑完全重复；用正则匹配"继续"关键词路由 resume；thinkingBlock 全局仅一个，切会话即丢；interrupt 状态切会话被清空 | `front/agent_front/src/...`（ChatView.vue:134-198/227-263/107/308、session.ts:38） |

**相对健康的部分（建议保留）**：prompts.py（8 个角色 prompt 质量高）、RAG 核心（父子分块 + BM25 + 重排 + 查询改写）、文档入库链路（MinIO → 切块 → PG 事务 → RabbitMQ → Milvus）、data/knowledge 领域知识库。

---

## 二、参考项目调研结论

你本地 `参考项目/` 目录里已经下载了大部分合适的参考仓库，结合网络调研，各候选项目的定位与可借鉴点如下：

| 项目 | Stars（约） | 技术栈 | 对本项目的价值 |
|------|-----------|--------|----------------|
| **langchain-ai/open_deep_research** | ~5k | Python + LangGraph | **后端编排的首选参考**：LangGraph 官方多 Agent 深研项目。其 `clarify_with_user` 用 `interrupt()` 做澄清式 HITL、`Command(resume=...)` 恢复、`init_chat_model(configurable_fields=...)` 按节点动态配模型、supervisor 委派子 Agent——恰好覆盖你所有跑不通的功能点 |
| **lobehub/lobe-chat**（已本地） | ~67k | Next.js/React | **对话产品形态的天花板**：多会话管理、消息渲染、思考过程折叠、Artifacts、知识库、多模型切换（DeepSeek/豆包式的产品体验） |
| **open-webui** | ~90k | Svelte | 自托管对话 UI 的另一个标杆，功能全但技术栈差异大，主要借鉴交互设计 |
| **LibreChat** | ~25k | React | 多模型路由（OpenRouter 式）、多用户、插件体系，借鉴其 API 层的模型路由设计 |
| **vercel/ai-chatbot** | ~21k | Next.js | **流式协议与前端数据流的教科书**：AI SDK 的 data stream protocol、流式消息状态机、持久化聊天记录，可直接借鉴事件协议设计 |
| **gpt-researcher**（已本地） | ~19k | Python | 深研流水线：多查询规划、引文标注、报告生成结构，借鉴其报告引用体系 |
| **langchain-ai/open-agent-platform** | ~1k | Next.js + LangGraph | LangChain 官方 Agent 平台：无代码改 prompt + HITL 审批 UI + LangGraph Server，是"前后端如何围绕 LangGraph 协作"的官方范本 |
| **langchain-ai/memory-template**（已本地） | ~1k | Python + langmem | 官方长期记忆模板：LangGraph Store + langmem 热路径/后台记忆整理，用来替换你的哈希向量记忆 |
| **langfuse**（已本地） | 万级 | TS + Python SDK | LLM 可观测性：trace 级调用链、token/成本统计，替换你手写的 trace.log |

**建议的借鉴分工**：后端编排照 open_deep_research + open-agent-platform；事件协议照 vercel/ai-chatbot（AI SDK data stream）；记忆照 memory-template；深研报告与引用照 gpt-researcher；产品交互形态照 lobe-chat；可观测性用 langfuse。

---

## 三、功能确认清单

以下按模块列出重构后系统的功能项。每项标注：**[处置]** 保留 / 重写 / 新增 / 删除，以及现状评级。请逐项确认或修改。

### A. 对话与会话管理

| # | 功能 | 现状 | 建议处置 |
|---|------|------|----------|
| A1 | 多会话（thread）列表、切换、重命名、置顶、删除 | 有，但前端 10 秒轮询刷新 | 保留后端 API，前端改为事件驱动刷新 |
| A2 | 会话历史消息持久化与回放 | 有（PG checkpointer） | 保留，补充"从任意历史点回看最终报告" |
| A3 | 会话标题自动生成 | 无（重命名靠手填） | 新增（lobe-chat/vercel 方式：首轮对话后 LLM 生成标题） |
| A4 | 回滚（rollback）到历史某个检查点 | 有 API | 保留，前端补 UI 入口 |

**待确认**：A3、A4 需要，多用户/登录体系不需要

### B. 流式输出（重灾区，全部重写）

| # | 功能 | 现状 | 建议处置 |
|---|------|------|----------|
| B1 | **token 级流式**：最终报告逐 token 打出（DeepSeek/豆包体验） | 无，报告一次性下发 | 重写：`astream_events` 的 `on_chat_model_stream` 直通 SSE |
| B2 | 统一事件协议：一套 JSON 事件（message_start / token / thinking / progress / tool_call / interrupt / done / error）贯穿前后端 | 无，事件零散 | 重写，参考 vercel AI SDK data stream protocol |
| B3 | 中间过程展示：当前在哪个 Agent、正在做什么（计划/检索/分析进度） | 仅进度文案 | 重写：节点级进度事件 + 前端时间线组件 |
| B4 | 思考过程（thinking）展示与折叠 | 前端只有一个全局 thinkingBlock，切会话即丢 | 重写：消息级 thinking 块，随会话持久化 |
| B5 | SSE 异常兜底：任何异常必须发 error 事件 + 关闭流，前端永不挂起 | 有 NameError 导致挂起的 bug | 重写（结构性修复，非补丁） |

**待确认**：保持 SSE + 独立 POST

### C. 中断 / 恢复 / 取

| # | 功能 | 现状 | 建议处置 |
|---|------|------|----------|
| C1 | 用户主动停止生成（豆包"停止"按钮） | 只在节点间隙生效 | 重写：取消信号 + LLM 调用级中断（task cancel） |
| C2 | 中断后从断点恢复（断点续研） | resume 会重跑全图、覆盖进度 | 重写：基于 checkpointer 的正确 resume |
| C3 | 服务重启后会话不丢（持久化挂起中的任务） | 部分 | 保留 PG checkpointer，补恢复逻辑 |

**待确认**：C2"断点续研"要做到（c）进程崩溃后也能续。

### D. HITL 人机协同（重灾区，重写）

| # | 功能 | 现状 | 建议处置 |
|---|------|------|----------|
| D1 | 研究计划审批：展示 plan，用户批准/修改/否决 | interrupt 已埋三处，但前端用正则匹配"继续"路由 | 重写：结构化审批 UI（卡片 + 按钮），参考 open_deep_research 的 clarify_with_user |
| D2 | 澄清式提问：Agent 主动向用户提问（多轮澄清研究范围） | 无 | 新增（open_deep_research 核心模式） |
| D3 | 最终报告审核：生成后可要求"再深入某方向" | 无 | 新增（reflect 环节接 HITL） |
| D4 | interrupt 状态持久化：切换会话再回来，审批框还在 | 切会话被清空 | 重写 |

**待确认**：D1 的"修改计划"需要支持 批准/修改/否决，输入框给出原因给节点重新生成计划

### E. 记忆系统（重灾区，重写）

| # | 功能 | 现状 | 建议处置 |
|---|------|------|----------|
| E1 | 短期记忆（会话内上下文） | 有，混在巨类里 | 重写：拆分为 LangGraph 原生 messages + checkpointer |
| E2 | 长期记忆（跨会话：用户偏好、研究历史） | MD5 哈希伪向量，检索=随机 | 重写：改用 langmem / LangGraph Store（memory-template 模式），embedding 换真实向量模型 |
| E3 | 记忆可见性：前端能查看/编辑"系统记住了什么" | 无 | 新增（lobe-chat 式记忆面板，可选） |

**待确认**：E3 需要，长期记忆的范围含跨 thread 的研究主题记忆

### F. RAG 与知识库

| # | 功能 | 现状 | 建议处置 |
|---|------|------|----------|
| F1 | 文档上传 → 解析 → 切块 → 向量化（MinIO + RabbitMQ + Milvus 链路） | 基本健康 | 保留，只做代码整理与错误重试补强 |
| F2 | 检索（父子分块 + BM25 + 重排 + 查询改写） | 设计良好 | 保留 rag/core.py |
| F3 | 网络检索与本地 RAG 并行 | 有（Bocha） | **重写**：删除 Bocha，换 DuckDuckGo（`duckduckgo-search`），抽 `SearchProvider` 抽象 + 缓存 + 失败降级 |
| F4 | **引用与溯源**：报告中的论断标注来源（网页/知识库文档），可点击跳转 | 无 | 新增（gpt-researcher 式引文体系，DeepSeek/豆包报告体验的关键） |

**待确认**：F4 引用溯源是这次重构新增的重点之一，按顺序做就行；Bocha 搜索这种 API 容易被墙，删除 → **替代方案已定为 DuckDuckGo（`duckduckgo-search` 库，无需 API key、无需自托管服务）**。

### G. 多智能体架构（核心资产，保留 + 微调）

| # | 功能 | 现状 | 建议处置 |
|---|------|------|----------|
| G1 | 图拓扑：intent → plan → [web_search ∥ local_rag] → deep_dive → analyze → reflect/write | 设计合理 | **保留** |
| G2 | 8 个角色 prompt | 质量高 | **保留** |
| G3 | State 结构 | 41 字段扁平 TypedDict，无校验 | 重写为带 reducer 与校验的结构（拆分：对话态/研究态/进度态） |
| G4 | 死代码清理 | 旧节点实现、codegen_node 未接线 | 删除 |
| G5 | 按节点动态配置模型（计划用强模型、压缩用快模型） | 无，全部用 ChatTongyi | 新增（open_deep_research 的 configurable_fields 模式） |

**待确认**：G5 需要接入哪些模型，按照推荐

### H. 研究产物与报告

| # | 功能 | 现状 | 建议处置 |
|---|------|------|----------|
| H1 | Markdown 报告渲染（代码块、表格、公式） | 手写渲染，混乱 | 重写：成熟渲染库（marked/markdown-it + highlight） |
| H2 | 报告导出（Markdown / PDF / Word） | 无 | 新增 |
| H3 | 研究过程时间线（看了哪些来源、每步结论） | 无 | 新增（进度事件的可视化沉淀） |

**待确认**：H2   Markdown PDF ，H3 新增

### I. 前端整体

| # | 事项 | 现状 | 建议处置 |
|---|------|------|----------|
| I1 | 技术栈 | Vue3 + 无状态管理 + 无 UI 库 | **关键决策，见第四节 Q1** |
| I2 | 消息流状态机 | SSE 解析散落在 ChatView | 重写：统一事件 reducer（无论选哪个栈） |
| I3 | 会话/审批/thinking 状态 | 模块级单例，切会话丢失 | 重写：纳入状态管理并持久化 |

### J. 工程化与可观测性

| # | 功能 | 现状 | 建议处置 |
|---|------|------|----------|
| J1 | LLM 调用链追踪（trace/成本） | 手写 trace.log | 新增：langfuse 自托管（本地已有仓库） |
| J2 | 测试 | 仅 test_multi_turn 等零散测试 | 新增：事件协议、resume、记忆检索的单测 |
| J3 | 配置管理 | .env + config.json 混杂 | 重写：统一 pydantic-settings |
| J4 | 部署 | docker-compose.middleware.yml | 保留，补全应用本身容器化 |

**待确认**：J1 langfuse 不纳入这次重构（可以后续再加）

---

## 四、关键决策（已于 2026-09-02 确认）

| # | 决策项 | 结论 |
|---|--------|------|
| 1 | 前端技术栈 | **继续 Vue3**，引入 Pinia + UI 组件库，借鉴 lobe-chat 交互自行重构 |
| 2 | 流式通道 | **SSE**（保持现有通道，重写事件协议） |
| 3 | 记忆方案 | **langmem + LangGraph Store**（官方路线，替换自研哈希向量方案） |
| 4 | 中间件 | **全保留**（RabbitMQ + MinIO + Milvus + Redis 链路健康，仅做代码整理） |
| 5 | HITL 范围 | **D1-D4 全做**（计划审批 + 澄清提问 + 报告审核 + interrupt 状态持久化） |
| 6 | 断点续研 | **c 档**：进程崩溃后也能基于 checkpointer 续跑 |

补充确认结论（来自各模块"待确认"的批复，已同步到计划 v1.1）：

7. **多用户与鉴权**：不做；API 层预留 user_id 字段（默认 anonymous），避免后续返工。
8. **模型接入（G5）**：按推荐——ChatTongyi(qwen) 为默认，通过动态模型配置支持任意 OpenAI 兼容 API（DeepSeek 等），不硬编码。
9. **langfuse 可观测性（J1）**：**不纳入本次重构**，后续需要时再接入。
10. **报告导出（H2）**：Markdown + PDF；H3 研究过程时间线新增。
11. **引用溯源（F4）**：纳入主线（Phase 7），按顺序做。
12. **Bocha 搜索**：**删除**（API 易被墙）。web_search 改接 **DuckDuckGo**（`duckduckgo-search` 库，免费无 key、纯库调用），接口层抽 `SearchProvider` 抽象保留换源能力。**v1.2 变更：原定「自托管 SearXNG」作废**——searxng 上游 `utils/templates/` 下 4 个文件名含 `:`，Windows git 索引构建时整树校验无法通过（已实测 `--depth 1` / `--sparse` / cone 模式均失败），故不再拉取该仓库，也不新增自托管搜索中间件。
13. **HITL D1 计划审批**：批准 / 修改（原因输入框，回 plan 节点重新生成）/ 否决三分支。
14. **A3 标题自动生成、A4 回滚入口**：均需要，纳入 Phase 6。

---

## 五、参考仓库与下一步

参考仓库已全部拉到本地 `参考项目/`，共 9 个（open_deep_research、ai-chatbot、open-agent-platform、memory-template、gpt-researcher、lobe-chat、langgraph、open-canvas、langfuse），后续开发无需到网络查找。**`searxng` 不再拉取**（见决策 12）。

完整清单、commit 版本与**关键文件入口路径**见《DeepResearch_参考仓库索引.md》；用途与对应 Phase 见《DeepResearch_重构详细计划.md》第一节。

详细重构计划见《DeepResearch_重构详细计划.md》（v1.1，含目标架构、目录结构、事件协议规范、分阶段任务拆解与验收标准）。

---

*附：本清单基于 2026-09-02 的代码走读（覆盖 app/backend、app/mult_agents、front/agent_front 全部核心文件）与 GitHub 开源项目调研。*
