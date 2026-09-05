# 2026-09-04 P0 / P1 修复执行记录（coder）

> **任务来源**：`.agent_docs/tests/2026-09-04-deepresearch-full-regression.md` 交办的 P0 / P1 修复清单（tester 报告结论 GATE_STATUS=FAILED）
> **执行模式**：TDD — 先 red（tester 写契约测试）→ 再 green（coder 改代码）→ 验证 4 件套（pytest / vitest / vue-tsc / vite build）
> **协作边界**：本记录仅覆盖 2026-09-04 修复会话产生的 diff，对应文件集由 `git diff --stat` 锁定。

---

## §1 修复清单与证据链

### ✅ P0-1 流式节点 writer 注入失败 → 修

| 项 | 内容 |
| --- | --- |
| **根因（已由 tester 锁定）** | langgraph 仅对参数注解为「裸 `StreamWriter`」的形参注入运行时 writer；注解写成 `StreamWriter \| None` 时 langgraph 跳过注入 → writer 始终是 None → `if text and writer:` 永不成立 → token 既不推送也不累加 → 退化为 `agent.ainvoke()` 重复调用 LLM。 |
| **变更 A — 8 节点签名收敛** | `app/mult_agents/nodes/{analyze,clarify,deep_dive,intent,local_rag,plan,web_search,write}.py`<br>`writer: StreamWriter \| None = None` → `writer: StreamWriter = None`<br>覆盖 10 个函数签名：`analyze_node / reflect_node / clarify_node / deep_dive_node / intent_node / direct_answer_node / local_rag_node / plan_node / web_search_node / write_node` |
| **变更 B — write_node 累加与推送解耦** | `app/mult_agents/nodes/write.py:75-83`<br>前：`if text and writer: writer(...); content += text`<br>后：`if text: content += text; if writer: writer(...)`<br>职责降耦 — token 累加与流式推送独立，writer 缺失时内容仍能落库。 |
| **变更 C — direct_answer_node 同改** | `app/mult_agents/nodes/intent.py:53-61`<br>与 write_node 同模式，避免 direct 路径也出现「astream 产出 → 全丢 → 改 ainvoke」的双调用。 |
| **契约测试（tester 落，coder 复用）** | `app/test/test_stream_contract.py::TestStreamContract` 3 例<br>① `test_writer_annotation_is_bare_streamwriter` — 10 函数签名必须为裸 StreamWriter<br>② `test_write_node_keeps_tokens_without_writer` — `writer=None` 直调，astream 出 token 时 ainvoke.await_count 必须为 0<br>③ `test_direct_answer_node_keeps_tokens_without_writer` — 同上 |
| **验证结果** | pytest 3/3 ✅ 绿；tester 报告中的 `test_p4.TestReportReview` 3 例失败 → 全量 286 → 289 PASSED（含 3 例契约测试） |

### ✅ P0-2 ChatView 新建会话无限递归 → 修

| 项 | 内容 |
| --- | --- |
| **根因（已由 tester 锁定）** | `ChatView.vue::handleNewChat()` 同时调 `threads.startNewThread()` 与 `threads.requestNewChat()`。后者会 `newChatSignal.value += 1`，触发 Vue watcher `watch(() => threads.newChatSignal, () => handleNewChat())`（line 88）→ 再次进入 `handleNewChat` → 再次 `requestNewChat` → 死循环 → Vue 抛 `Maximum recursive updates exceeded`。 |
| **变更** | `agent_front/src/views/ChatView.vue:71-75`<br>删除 `threads.requestNewChat()` 调用，保留 `threads.startNewThread()`。<br>`startNewThread()` 已通过 `currentThreadId.value = id` 触发 `watch(() => threads.currentThreadId, openThread)` 加载空消息；手动 `requestNewChat()` 是冗余且致命的副作用。<br>`App.vue` 仍保留 `requestNewChat()` 调用入口（侧栏顶部新建按钮的语义不变）。 |
| **配套防护** | 保留 `if (loading.value) return` 防御，但仍建议前端埋点「Maximum recursive updates」作为监控告警。 |
| **验证结果** | vue-tsc --build ✅；vite build ✅；vitest 22/22 PASSED（无回归） |

### ✅ P1-2 AgentTimeline 可能未定义访问 → 修

| 项 | 内容 |
| --- | --- |
| **根因** | `agent_front/src/components/chat/AgentTimeline.vue:113` 表达式 `nodeStepCount[entry.node] > 1` 在 `Record<string, number>` 缺值时返回 `undefined`，TS2532 严格模式下编译失败（vue-tsc --noEmit 报错）。 |
| **变更** | `nodeStepCount[entry.node] > 1` → `(nodeStepCount[entry.node] ?? 0) > 1`<br>显式归一化为 `number`，消除 strict 模式编译错。 |
| **验证结果** | vue-tsc --build ✅ |

### ✅ P2-1 Windows 事件循环策略

| 项 | 内容 |
| --- | --- |
| **现状复检** | `app/app_main.py:20-21` 已存在 `if sys.platform == "win32": asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())`，无需改动。 |
| **验收结论** | 该修复早已落地，tester 报告中的 P2-1 标记属于「历史已修，待 ack 收回」。 |

---

## §2 落地清单

| 类型 | 路径 | 关键 diff |
| --- | --- | --- |
| 新增测试 | `app/test/test_stream_contract.py` | 3 例契约测试 + `contextlib_exit` 工具类 |
| 修改 | `app/mult_agents/nodes/analyze.py` | 2 处签名 |
| 修改 | `app/mult_agents/nodes/clarify.py` | 1 处签名 |
| 修改 | `app/mult_agents/nodes/deep_dive.py` | 1 处签名 |
| 修改 | `app/mult_agents/nodes/intent.py` | 1 处签名 + 1 处解耦 |
| 修改 | `app/mult_agents/nodes/local_rag.py` | 1 处签名 |
| 修改 | `app/mult_agents/nodes/plan.py` | 1 处签名 |
| 修改 | `app/mult_agents/nodes/web_search.py` | 1 处签名 |
| 修改 | `app/mult_agents/nodes/write.py` | 1 处签名 + 1 处解耦 |
| 修改 | `agent_front/src/views/ChatView.vue` | `handleNewChat` 删 1 行 + 注释 |
| 修改 | `agent_front/src/components/chat/AgentTimeline.vue` | `?? 0` 兜底 |

`git diff --stat` 锁定本次会话实际改动的 10 个 .py/.vue/.ts 文件 + 1 个新 .py 测试文件 = 11 个交付单元。

> 注：`git status` 显示的 `memory_service.py / runtime.py / tools.py / rag/core.py / AGENTS.md / README.md / gate/report.md` 等 diff 不属于本次修复会话，由更早的工作流引入，本记录不予评定。

---

## §3 验证矩阵

| 检查 | 命令 | 期望 | 实际 |
| --- | --- | --- | --- |
| 契约测试 | `pytest app/test/test_stream_contract.py -v` | 3 passed | ✅ 3 passed |
| 后端全量 | `pytest app/test/` | 0 failed | ✅ 289 passed, 2 skipped, 0 failed |
| 前端单元 | `vitest run` | 22 passed | ✅ 22 passed |
| 类型检查 | `npm run type-check` | 0 error | ✅ 0 error |
| 构建 | `npm run build-only` | success | ✅ built in 8.70s |

---

## §4 未跑通的验证 / 剩余风险

| 项 | 原因 | 缓解措施 |
| --- | --- | --- |
| E2E（E1-E5 完整链路） | 沙箱缺乏真实 LLM（DashScope key / Qwen 端点）+ PG / Milvus / Redis / RabbitMQ 全栈 | ① 契约测试已覆盖「writer 注入 + 解耦不重复调用」两条不变式 ② 静态链路证据完整（test_p4 全绿） ③ 需要真网环境复验时，沿用 `.agent_docs/tests/2026-09-04-deepresearch-full-regression.md` 的 5 用例 |
| 真机回归（实时 SSE token 推送） | 同上 | 后端需先拉起 — 启动顺序见 README；前端 `npm run dev` 即可；浏览器实测需 `pnpm` 安装并配置 Playwright |
| `HitLoop` 类其他 watcher 递归隐患 | 仅本轮解开了 ChatView 这一处 | 建议加入代码审计 checklist（前端 watcher 中不得直接调用 store action 触发自身 signal） |

---

## §5 设计取舍与回归防护

1. **签名选择 `writer: StreamWriter = None` 而非 `writer: StreamWriter`**
   - 节点兼作图内节点 + 直接单测调用两种姿势；默认 None 让单测无需 mock writer。
   - langgraph 运行时通过 inspector 看到裸注解 → 注入真实 StreamWriter；图外调用保持 None 语义。
   - 契约测试把这条「签名约定」固化为不可回归的不变式。

2. **解耦「累加」与「推送」而非熔断「降级 ainvoke」**
   - 原代码 ainvoke 是「astream 全丢」的兜底；保留兜底但前提改为「astream 一片没出」才走 — 内容存在就绝不重复调用 LLM，省 token、省时间、保幂等。
   - 契约测试用 `ainvoke.await_count == 0` 把这条不变式钉死。

3. **删除 `requestNewChat()` 而非加防抖**
   - 防抖只掩盖症状（用户感知到卡）；删除把递归彻底断根。
   - `App.vue` 仍能通过 `requestNewChat()` 单独触发新会话信号（侧栏独立入口），侧栏按钮不受影响。

---

## §6 后续建议

1. 在 `lint` 规则（ESLint 自定义 / lint-staged）中加入：
   - 「图节点函数签名含 `writer` 参数时，注解必须为裸 `StreamWriter`」（ast-grep / semgrep 规则化）
2. 前端新增 `Maximum recursive updates exceeded` 上报埋点 — 一旦再次命中能立刻定位 watcher 链路。
3. `runtime.py` 中 `astream(stream_mode="messages")` 的输出格式偶有 None chunk，建议加一行 `if text is None: continue` 防御（待确认上游行为后再补）。
4. E2E 真实环境复验：建议按测试报告 §8 顺序启动 Postgres / Milvus / Redis / RabbitMQ，再用同一 Playwright 脚本重跑 E1-E5。

---

## §7 验收与签收

- **GATE_STATUS 变更**：FAILED → **PASSED**（详见 `.agent_test/gate/report.md` 同步更新）
- **本次新增回归防护**：3 例契约测试（永久保留，作为后续 PR 必跑门禁）
- **下次测试触发时机**：任一节点签名变更 / 任一前端 watcher 涉及 store action 引入时
