# DeepResearch 全量回归测试报告

- **执行日期**：2026-09-04
- **被测对象**：DeepResearch（FastAPI + LangGraph 后端 `app/`，Vue3 + Pinia 前端 `agent_front/`）
- **Commit**：`cfef2381de8c2c0dfca67910615cf01ce4755b91`（工作区含未提交变更，见「变更影响面」）
- **执行人**：tester agent（`C:\Users\20448\.ai-shared\agents\tester.md`）

---

## 一、环境探测

| 项 | 实测值 |
|---|---|
| 后端运行时 | conda `llmdev` — Python 3.11.15（`D:\develop_tools\miniconda3\envs\llmdev`） |
| 测试框架 | pytest 9.1.1（`asyncio_mode=auto`，`addopts=-m "not llm and not slow"`） |
| 前端运行时 | Node v22.22.2 / npm 10.9.7，vitest 4.1.11（jsdom） |
| E2E 引擎 | Playwright 1.62.0（隔离 venv）+ 系统 Edge（`channel="msedge"`） |
| Docker 中间件 | postgres / redis / milvus / etcd / minio / rabbitmq 全部 `Up 2 hours` |
| 中间件端口 | 5432 / 6379 / 19530 / 5672 均 TCP OPEN |
| API Key | `.env` 中 `DASHSCOPE_API_KEY` 已配置 |

> 端口首次用 curl 探测时 5432/6379 误报 closed（HTTP 协议不匹配导致非 0 退出），已改用 TCP connect 复核为 OPEN。

## 二、测试策略与范围

| 层次 | 范围 | 方式 | 结果 |
|---|---|---|---|
| 后端单元 / 集成 | `app/test/` 全部 16 个文件 | pytest 全量 | 283/288 通过 |
| 前端单元 | `agent_front/test/` 2 个文件 | vitest run | 22/22 通过 |
| 类型检查 | 全量 TS + Vue SFC | `vue-tsc --build` | ❌ 1 error |
| 构建验证 | 前端生产构建 | `vite build` / `npm run build` | build ✅ / 整体 ❌ |
| 浏览器 E2E | 关键用户路径 E1–E5 | Playwright + msedge | 2/5 通过 |

## 三、用例清单矩阵

| 测试套件 | 用例数 | 覆盖对象 |
|---|---|---|
| `test_integration_audit.py` | 45 | 前后端契约联调（HITL kind / payload 校验 / 依赖与死代码） |
| `test_p4.py` | 38 | HITL 三点中断（clarify / plan_approval / report_review） |
| `test_p5.py` | 27 | 记忆系统（langmem + Store）与 runtime 装配 |
| `test_hitl.py` | 26 | HITL 回归 |
| `test_citations.py` | 22 | 引用去重与编号 |
| `test_resume.py` | 17 | 断点续研 |
| `test_summary.py` | 17 | 摘要与报告后处理 |
| `test_stream_events.py` | 15 | 事件协议流式 |
| `test_events.py` | 14 | 事件 schema 注册 |
| `test_memory.py` | 14 | 记忆模块结构与清理 |
| `test_p3.py` | 13 | 任务取消与 TaskRegistry |
| `test_p1.py` | 10 | 拓扑 / reducer / 搜索 |
| `test_p2.py` | 10 | 流式输出 |
| `test_state.py` | 9 | State 契约 |
| `test_search_provider.py` | 9 | DuckDuckGo 搜索 Provider |
| `test_p1_smoke.py` | 2 | 端到端冒烟（需真实 LLM + PG） |
| **合计** | **288** | — |

前端：`test_p6.test.ts`（10）、`test_p7.test.ts`（12），合计 22。
E2E：E1 页面可达 / E2 健康检查 / E3 提问建流 / E4 HITL 中断卡 / E5 任务取消。

## 四、执行结果

### 后端 pytest（9.72s）

| 指标 | 数量 |
|---|---|
| 总用例 | 288 |
| 通过 | 283 ✅ |
| 失败 | 3 ❌ |
| 跳过 | 2 ⏭️（`test_p1_smoke.py` 2 例，已知跳过：需真实 LLM + PG） |

### 前端 vitest（3.63s）

22/22 通过，0 失败。

### 构建验证

| 检查 | 结果 |
|---|---|
| `npx vite build`（仅打包） | ✅ built in 8.64s |
| `npm run type-check`（vue-tsc） | ❌ `src/components/chat/AgentTimeline.vue(113,78): error TS2532: Object is possibly 'undefined'` |
| `npm run build`（type-check + build-only） | ❌ exit=1（被 type-check 阻塞） |

### 浏览器 E2E（E1–E5）

| # | 用例 | 结果 | 耗时 | 说明 |
|---|---|---|---|---|
| E1 | 页面可达无 console 报错 | ✅ | 1.4s | title 正确，0 console error |
| E2 | GET /health/live | ✅ | 0.0s | `{"status":"ok"}` |
| E3 | 提问建流渲染助手回复 | ❌ | 151.5s | 后端 9.8s 完成（final_len=286），但 UI 无回复气泡（见 P0-1/P0-2） |
| E4 | HITL 计划审批中断卡 | ❌ | 181.4s | 前端已冻结（Vue recursive updates），请求未发出 |
| E5 | 任务取消 | ⏭️ | — | 依赖 E4 运行态，被阻塞 |

证据：`output/e2e_01_home.png`、`output/e2e_E3_fail.png`、`output/e2e_E4_fail.png`、`output/e2e_results.json`（含 SSE 请求与 pageerror 记录）、复现脚本 `output/e2e_run.py`。E2E 结束后 8000/5173 服务已确认停止，无孤儿进程。

## 五、失败详情与根因分析

### 根因总表（按优先级）

| 优先级 | 缺陷 | 位置 | 根因（实测证据） | 严重程度 |
|---|---|---|---|---|
| **P0-1** | **流式节点 StreamWriter 注入失败，token 全部丢弃 → 前端永远收不到回复内容** | `app/mult_agents/nodes/write.py:25`、`app/mult_agents/nodes/intent.py:47`（direct_answer_node，`write.py:81` / `intent.py:59` 同款 `if text and writer:` 耦合） | 节点签名 `writer: StreamWriter \| None = None` **带默认值，langgraph 不注入 StreamWriter**（探针实证：带默认值→注入 False；去默认值→注入 True，partial 包装不影响）。writer 恒 None → astream token 全丢弃 → 走降级 ainvoke → SSE 实测 direct 路由仅 `run.started + 2×agent.status + run.completed`，**0 个 message.delta** → 前端 reducer（`useEventStream.ts:46` appendDelta）无内容可渲染 | Critical |
| **P0-2** | **ChatView 响应式死循环导致应用冻结** | `agent_front/src/views/ChatView.vue`（pageerror ×2） | `Maximum recursive updates exceeded in component <ChatView>`：存在自我触发的响应式 effect（嫌疑：computed 链 `getMessages→getThread` 惰性写 reactive Map、`run.completed→threads.refresh` 与 watcher 链相互触发）。实测 E3 结束后点击新建会话/卡片无任何响应，E4/E5 被阻塞 | Critical |
| **P1-1** | test_p4 3 例失败（回归） | `app/test/test_p4.py:451/473/495`，`write.py:85` | `TypeError: object MagicMock can't be used in 'await'`：`invoke→await ainvoke` 契约变更后 mock 未适配。探针 A 实证 HEAD 版同步路径返回 str 可走到断言 → **判定为本次变更引入的回归**。P0-1 修复后 writer 注入成功、流式 content 非空，不再走降级分支，3 例预期自然恢复 | Major |
| **P1-2** | 前端构建失败（既有问题，非本次变更） | `AgentTimeline.vue:113:78` TS2532 | `npm run build` 被 type-check 阻塞（exit=1）。git status 显示前端无本次改动 → 既有缺陷 | Major |
| **P2-1** | Windows 记忆系统初始化失败 | `app_main`（启动日志） | `psycopg.pool: Psycopg cannot use the 'ProactorEventLoop' to run in async mode` → P5 记忆初始化失败（不阻塞启动）。修复方向：`asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())` | Minor |
| **P2-2** | PG checkpointer 实际失效 + 零测试覆盖 | `app/mult_agents/runtime.py`（本次 +74 行） | 实测同步 `PostgresSaver`/`RedisSaver` 自身不定义 `aget_tuple/aput/aput_writes`（`AsyncPostgresSaver` 才有）→ 降级判定恒真 → checkpointer 恒为 `InMemorySaver`，跨进程续研/回滚失效。判定逻辑本身正确，但应改用 `AsyncPostgresSaver`，且 pytest 套件对该函数零覆盖 | Minor |

### F1–F3 失败详情（后端 pytest）

| # | 用例（文件:行） | 错误 | 严重程度 |
|---|---|---|---|
| F1 | `test_report_review_adopt`（`app/test/test_p4.py:451`） | `TypeError: object MagicMock can't be used in 'await' expression`（`write.py:85`） | Major |
| F2 | `test_report_review_deepen_within_limit`（:473） | 同上 | Major |
| F3 | `test_report_review_deepen_at_max_force_adopt`（:495） | 同上 | Major |

### 关键取证记录

1. **探针 A**（HEAD 降级路径）：`_last_content(MagicMock.invoke(...))` 返回 `str`（`'<MagicMock …'`）→ 改动前测试可走到断言。
2. **探针 B**（当前路径）：`await MagicMock().ainvoke(...)` → `TypeError`。
3. **探针 C/D**（writer 耦合）：writer=None 时 astream 产出「流式正文」但 content 为空、降级 `ainvoke` 调用 **1 次**；writer 存在时降级调用 **0 次**。
4. **StreamWriter 注入探针**（零 LLM 成本）：`writer: StreamWriter | None = None` → 不注入；`writer: StreamWriter`（无默认值）→ 注入，`partial` 包装不影响。
5. **SSE 抓包**（curl POST /api/v1/research/stream，真实调用）：事件统计 `run.started ×1、agent.status ×2、run.completed ×1`，**无 message.start / message.delta**；后端日志 `final_len=286` 正常完成。
6. **E2E 截图**：执行进度（intent ✔ / direct_answer ✔）渲染正常，助手回复气泡缺失。

### 回归 vs 已知失败

- F1–F3：**本次变更引入的回归**（证据 1/2）。
- 2 例 skip：已知跳过（README 记录一致）。
- P1-2（TS2532）、P2-1（ProactorEventLoop）：既有问题，非本次变更。

## 六、修复建议（归 coder）

1. **P0-1**：全部流式节点签名改为 `writer: StreamWriter`（去默认值，langgraph 才注入）——排查 `write.py`、`intent.py(direct_answer_node)`、`analyze.py`、`plan.py`、`local_rag.py` 等所有使用 writer 的节点；同时将 `if text and writer:` 拆为「先累加、writer 存在才推送」的防御写法。
2. **P0-2**：定位 ChatView 自触发响应式 effect（优先排查 `chat.ts getThread` 在 computed 求值路径上的 `threads.set()` 写入，改为显式初始化）；增加 `onErrorCaptured` 或 Vue warn 上报避免静默冻结。
3. **P1-1**：`test_p4.py` 三处 mock 补 `AsyncMock.ainvoke`（P0-1 修复后的兜底）。
4. **P1-2**：修复 `AgentTimeline.vue:113` 空值访问。
5. **P2-1**：Windows 启动入口设置 `WindowsSelectorEventLoopPolicy`。
6. **P2-2**：改用 `AsyncPostgresSaver` / `AsyncRedisSaver`，并为 `build_checkpointer` 补降级路径单测。

## 七、覆盖缺口与风险（非失败项）

| 风险 | 说明 | 建议 |
|---|---|---|
| `runtime.py` 74 行改动零测试覆盖 | `build_checkpointer` 新逻辑无任何 pytest 用例（仅 `eval_metrics.py` 评测脚本引用） | coder 补单测 |
| `intent.py`、`rag/core.py` 改动无直接测试 | `tools.py` 有 9 例覆盖，`intent.py`/`rag/core.py` 无对应用例 | 补节点级单测 |
| 事件协议契约无端到端断言 | `message.delta` 缺失这类前后端契约断裂，单测全绿也无法发现（本次 E3 即为例证） | 增加「SSE 事件类型序列」断言的集成测试 |

## 八、最终评价

- **测试健康度**：🔴 **不健康**
  - 单测层面 283/288 + 22/22 看似良好，但 E2E 暴露 **P0 级用户可见缺陷：提问后界面永远不显示回复**（后端正常完成、SSE 无内容事件、前端死循环冻结），单测未能覆盖该契约链路；
  - `npm run build` 被类型错误阻塞；
  - checkpointer 关键路径零覆盖且 PG 持久化实际失效。
- **可否合并**：❌ **不可合并**。存在 2 项 P0（流式 token 全丢弃 / 前端死循环冻结）+ 构建失败。
- **改进建议**：
  1. 流式节点统一「writer 必填注入 + 累加与推送解耦」约定，并加一条「writer 非 None」断言的节点级单测；
  2. 增加 SSE 事件序列契约测试（至少断言 direct 路由含 `message.start→message.delta→run.completed`）；
  3. `npm run type-check` 纳入门禁。
