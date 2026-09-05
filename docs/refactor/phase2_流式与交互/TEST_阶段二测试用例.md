# 阶段二测试用例（流式与交互）

> 配套任务：R2.1 SSE 心跳保活 / R2.2 断线重连与续流 / R2.3 LLM 驱动澄清 / R2.4 thinking 流式透传
> 优先级：P0 = 验收必过；P1 = 应尽量通过；P2 = 建议覆盖
> 通用规约：后端单测不依赖真实 DashScope/PG/Redis，一律 mock；前端测试基于 Vitest + jsdom（`agent_front/test/`）。

## 1. 测试文件规划

| 测试文件 | 覆盖任务 | 端 |
| --- | --- | --- |
| `app/test/test_sse_heartbeat.py`（新增） | R2.1 | 后端 |
| `agent_front/test/test_sse_heartbeat.test.ts`（新增） | R2.1 | 前端 |
| `agent_front/test/test_reconnect.test.ts`（新增） | R2.2 | 前端 |
| `app/test/test_thread_routes.py`（新增） | R2.2 | 后端 |
| `app/test/test_clarify_llm.py`（新增） | R2.3 | 后端 |
| `agent_front/test/test_clarify_card.test.ts`（扩展） | R2.3 | 前端 |
| `app/test/test_thinking_stream.py`（新增） | R2.4 | 后端 |

## 2. T2.1 SSE 心跳保活（R2.1）

### T2.1-01 空闲超时产出心跳【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 构造 async generator：先产出 `("custom", {...})`，随后 `asyncio.sleep(0.05)` 再产出第二项；`interval=0.02` |
| 步骤 | 收集 `_astream_with_heartbeat(gen, 0.02)` 全部产出 |
| 预期 | 产出序列中至少含一次 `("heartbeat", None)`，且位于第一项与第二项之间；两个业务 chunk 均完整透传 |

### T2.1-02 超时后不丢失/不重复 chunk【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | generator 产出 10 个业务 chunk，每两个之间 sleep 超过 interval |
| 步骤 | 收集全部产出，比对业务 chunk |
| 预期 | 业务 chunk 恰好 10 个、顺序不变、无重复；心跳数量 ≥ 9（每段空闲一个） |

### T2.1-03 interval=0 关闭心跳【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | interval=0，业务 chunk 含长间隔 |
| 步骤 | 收集产出 |
| 预期 | 无 `heartbeat` 产出，行为等价直接透传 |

### T2.1-04 取消传播【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 外层 `task = asyncio.create_task(collect(...))`；generator 永不产出（await 挂起） |
| 步骤 | `task.cancel()` 后 `await task`，断言抛 `CancelledError` |
| 预期 | CancelledError 正常传播；内部 future 已清理（无 "Task was destroyed" 警告） |

### T2.1-05 stream_research 集成心跳帧格式【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | mock `self._app.astream` 为慢速 generator；`sse_heartbeat_seconds` 设为极小值 |
| 步骤 | 收集 `stream_research` 的 SSE 输出字符串 |
| 预期 | 输出中混有 `": ping\n\n"` 帧与 `data: {...}\n\n` 帧；心跳帧不以 data 开头；`run.started` 仍为第一帧、`run.completed`/`run.error`/`run.cancelled` 之一仍为终帧 |

### T2.1-06 前端忽略注释帧【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 构造 `": ping\n\ndata: {合法JSON}\n\n: ping\n\n"` 混合流（mock fetch Response，参考 agent_front/test 现有 SSE 测试的 mock 方式） |
| 步骤 | `consumeSSE(resp, onEvent)` |
| 预期 | onEvent 恰好被调用 1 次且收到该 JSON；注释帧无副作用 |

## 3. T2.2 断线重连与续流（R2.2）

### T2.2-01 网络错误触发重连【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock postStream 第一次抛 `TypeError("fetch failed")`（网络错误）；第二次返回正常 SSE 流（含 `run.started` → `run.completed`） |
| 步骤 | 调 `runWithReconnect(threadId, payload)`（退避 delay mock 为 0） |
| 预期 | 第二次调用 resume 接口（URL 含 resume 且 body.mode == 'continue'）；事件正常分发；reconnecting 状态最终为 false |

### T2.2-02 AbortError 不重连【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock postStream 抛 AbortError；chat store 未置 cancelled |
| 步骤 | 调 `runWithReconnect` |
| 预期 | 不发起任何后续请求（fetch 调用总数为 1） |

### T2.2-03 用户已取消不重连【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | chat store 置 `cancelled=true`；postStream 抛普通网络错误 |
| 步骤 | 调 `runWithReconnect` |
| 预期 | 无重连、无 state 查询请求 |

### T2.2-04 awaiting_input 状态停止重连【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock fetchThreadState 返回 `{status:'awaiting_input', resumable:true}` |
| 步骤 | 触发一次网络错误后等待 |
| 预期 | 调用了 state 查询；未调 resume postStream；reconnecting 复位为 false |

### T2.2-05 重连次数上限【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock postStream 每次均抛网络错误；fetchThreadState 返回 `{status:'idle', resumable:true}`；delay mock 为 0 |
| 步骤 | 调 `runWithReconnect` 并等待结束 |
| 预期 | resume 请求恰好 5 次（含首次共 6 次流请求——按实现定义计数，以「重试 5 次」为准断言）；最终 `markError` 被调用；reconnecting 复位 |

### T2.2-06 指数退避节奏【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | mock 定时器（vi.useFakeTimers）；连续失败 |
| 步骤 | 断言每次重试前的 sleep 时长 |
| 预期 | 序列为 1000、2000、4000、8000、16000（ms） |

### T2.2-07 消息对齐 replaceThreadMessages【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | chat store 中 thread 有 3 条消息（含 1 条 streaming 半截）；mock fetchThreadMessages 返回 2 条完整消息 |
| 步骤 | 调 `syncThreadMessages(threadId)` |
| 预期 | store 中该 thread 消息列表整体替换为 2 条；无 streaming 残留状态 |

### T2.2-08 sources.found 幂等去重【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 同一 sources 事件（两条 source：url+title 相同、chunk_id 相同）dispatch 两次 |
| 步骤 | 检查 chat store（或 sources store）中该 thread 的 sources 数组 |
| 预期 | 数量不翻倍（去重生效） |

### T2.2-09 message.start 替换同 node 旧 streaming 消息【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | store 中存在 node='write'、streaming=true 的旧消息 mid_old |
| 步骤 | dispatch `message.start`（mid_new，node='write'） |
| 预期 | 旧消息被移除，新消息插入且 streaming=true；后续 mid_new 的 delta 只追加到新消息 |

### T2.2-10 后端 state/messages 只读路由【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | FastAPI TestClient（参考现有 router 测试模式，中间件 mock） |
| 步骤 | `GET /api/v1/research/threads/{id}/state` 与 `GET /api/v1/research/threads/{id}/messages` |
| 预期 | 200；state 含 `status`/`resumable`/`interrupted_by_restart` 字段；messages 返回 `[{role, content}]` 列表 |

## 4. T2.3 LLM 驱动澄清（R2.3）

### T2.3-01 规则快速通道行为不变【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | query="最近的一些进展"（含模糊词且 <5 字符规则场景各测一组） |
| 步骤 | 调 clarify_node（agent 传 None 亦可命中） |
| 预期 | 直接发起 clarification 中断，LLM 未被调用（mock agent 的 invoke 计数为 0） |

### T2.3-02 LLM 判定需澄清【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock agent.invoke 返回 `{"needs_clarification":true,"confidence":0.3,"reason":"范围不明","questions":[{"question":"时间范围？","options":["近一年","不限"]}]}` |
| 步骤 | 调 clarify_node |
| 预期 | 发起 clarification 中断，payload.questions[0].options 长度 2 |

### T2.3-03 LLM 判定无需澄清直接进 plan【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock 返回 `needs_clarification=false, confidence=0.9` |
| 步骤 | 调 clarify_node |
| 预期 | 不发起中断，流转 plan（返回 Command(goto='plan') 或等效状态更新，按现有实现断言） |

### T2.3-04 LLM 异常静默放行【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock agent.invoke 抛异常；query 不命中规则 |
| 步骤 | 调 clarify_node |
| 预期 | 不抛异常、不发中断、进入 plan |

### T2.3-05 JSON 解析容错【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock 返回 `"```json\n{...合法JSON...}\n```"` 与纯文本两种 |
| 步骤 | 解析函数分别调用 |
| 预期 | 前者成功解析；后者返回 None（放行） |

### T2.3-06 回答充分性判定与追问【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | state.clarify_rounds=1（已问 1 轮）；mock 充分性判定返回 `sufficient=false` 且带 followup_questions |
| 步骤 | resume 后再次进入 clarify_node |
| 预期 | 再次发起澄清中断（问题来自 followup_questions），clarify_rounds 递增为 2 |

### T2.3-07 轮次上限放行【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | state.clarify_rounds=2（达上限 `clarify_max_rounds=2`）；LLM 判定仍需澄清 |
| 步骤 | 调 clarify_node |
| 预期 | 不再发起中断，直接进入 plan |

### T2.3-08 agent 注入【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | build_agents 构建 AgentBundle |
| 步骤 | 检查 bundle 中 clarify agent 存在且模型为 qwen-turbo、温度 0.0 |
| 预期 | graph 中 clarify 节点拿到该 agent |

### T2.3-09 前端 ClarifyCard 选项渲染【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 挂载 ClarifyCard，payload.questions 含 options |
| 步骤 | 渲染后点击一个选项 chip |
| 预期 | 选项展示为可点击元素；点击后对应输入框值为该选项文本；options 为空数组时不渲染选项区（行为同旧版） |

## 5. T2.4 thinking 流式透传（R2.4）

### T2.4-01 custom thinking 事件转发【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock astream 产出 `("custom", {"type":"thinking","node":"write","text":"推理片段"})` |
| 步骤 | 收集 stream_research 输出 |
| 预期 | 输出含 `message.thinking` 事件行；data JSON 含 `message_id`（`{run_id}:write`）与 `text=="推理片段"` |

### T2.4-02 thinking 先于 token 时 message.start 触发【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock astream 首个产出即为 thinking，随后才是 token |
| 步骤 | 收集输出，检查事件序列 |
| 预期 | 首个业务事件是 `message.start`（thinking 分支触发），随后 message.thinking，再 message.delta；不出现无 start 的孤儿 delta/thinking |

### T2.4-03 thinking_nodes 配置驱动【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | `thinking_nodes=[]` 构建 AgentBundle |
| 步骤 | 检查各 agent 构建参数 |
| 预期 | 全部 agent 的 enable_thinking 为 False（构建路径与旧代码一致——通过构建参数断言） |

### T2.4-04 reasoning 字段捕获【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 构造 mock LLM chunk：`additional_kwargs={"reasoning_content":"步骤一"}`、`content=""`；再一个 `content="正文"`、无 reasoning |
| 步骤 | 过节点推流逻辑（或提取出的捕获函数） |
| 预期 | 第一 chunk 仅产出 thinking 推送；第二 chunk 仅产出 token 推送 |

### T2.4-05 thinking 文本上限截断【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 持续推送 thinking 增量累计超过 32k 字符（mock 大量 chunk） |
| 步骤 | 统计实际转发的 thinking 事件文本总量 |
| 预期 | 转发总量被截断在 32k 附近；正文 token 不受影响；有 info 日志 |

### T2.4-06 resume_stream 的 thinking 分支【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 同 T2.4-01 但走 resume_stream（mode=continue） |
| 步骤 | 收集输出 |
| 预期 | resume 链路同样转发 message.thinking（两条链路行为一致） |

### T2.4-07 前端 thinking 分发【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | mock useEventStream.dispatch 一个 `message.thinking` 事件 |
| 步骤 | 检查 chat store |
| 预期 | 对应消息的 thinking 文本追加（或 thinking 面板状态更新），正文 content 不被污染 |

### T2.4-08 事件协议零变更【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 读取 `app/backend/schemas/events.py` 与 `docs/event-protocol.json` |
| 步骤 | diff 检查 |
| 预期 | EVENT_REGISTRY 仍为 10 种事件；MessageThinkingData 字段仍为 `(message_id, text)`；本任务未改动协议文件 |

## 6. 集成与浏览器自动化验证（阶段二收尾统一执行）

> 以下用例通过 Playwright 浏览器自动化执行，脚本存放于 `agent_front/e2e/phase2_streaming.spec.ts`。
> 前置：后端服务运行在 `http://127.0.0.1:8000`，前端运行在 `http://localhost:5173`（或 8080）。
> 选择器参考：`textarea.composer-input`（输入框）、`button.send-btn`（发送）、`.stop-button`（停止）、`.clarify-card`（澄清卡）、`.thinking-block`（思考面板）、`.message-item`（消息气泡）、`.agent-timeline`（进度时间线）。

| 编号 | 场景 | 自动化步骤 | 预期 |
| --- | --- | --- | --- |
| M2-01 | 心跳保活 | 1. 导航至 `/chat` 页面<br>2. 在 `textarea.composer-input` 输入一个深度研究问题<br>3. 点击 `button.send-btn` 发送<br>4. 通过 `page.route` 拦截 SSE 请求 (`**/api/v1/research/threads/*/stream`)，记录响应 chunk<br>5. 等待 `.agent-timeline` 出现 `deep_dive` 节点后检查拦截到的 SSE 数据<br>6. 断言 SSE 数据中含 `: ping` 注释帧（每 15s 出现一次）<br>7. 等待最终收到 `run.completed` 事件（`.message-item.assistant` 渲染完成） | 长静默期（deep_dive）每 15s 出现一条 `: ping` 注释帧；连接不被掐断；最终收到 run.completed |
| M2-02 | 断线重连 | 1. 导航至 `/chat` 页面，输入深度研究问题并发送<br>2. 等待 SSE 流开始（`.agent-timeline` 出现进度）<br>3. 通过 `page.context.setOffline(true)` 模拟断线 10s<br>4. 恢复网络 `page.context.setOffline(false)`<br>5. 等待页面出现重连提示文本（检查 `.message-list` 中是否含「正在恢复」或等效文案）<br>6. 拦截网络请求，断言后续发起了 state + messages + resume 请求<br>7. 等待 `.message-item.assistant` 内容补充完整<br>8. 检查消息文本无重复、`.source-list .source-item` 无重复条目 | UI 显示「正在恢复」；自动调用 state + messages + resume；报告从中断处继续；消息无重复文本、sources 无重复条目 |
| M2-03 | thinking 透传 | 1. 导航至 `/chat` 页面，输入深度研究问题并发送<br>2. 等待 `.agent-timeline` 出现 `write` 节点<br>3. 检查 `.thinking-block` 可见且含 `.thinking-label` 文本为「思考中...」<br>4. 检查 `.thinking-block` 内含流式追加的推理文本（`.log-msg` 非空）<br>5. 检查 `.message-item.assistant` 的正文内容独立于 thinking 文本<br>6. 点击 `.thinking-header` 可折叠/展开 | 思考面板流式展示推理文本（渐隐/可折叠），正文 token 独立流式渲染 |
| M2-04 | LLM 澄清 | 1. 导航至 `/chat` 页面<br>2. 在 `textarea.composer-input` 输入「帮我研究一下智能体框架」（模糊需求）<br>3. 点击 `button.send-btn` 发送<br>4. 等待 `.clarify-card` 出现<br>5. 检查 `.clarify-card` 内含 `.question-item` 且有 `.question-label`<br>6. 在澄清卡的 `NInput`（`textarea`）中输入回答<br>7. 点击「提交回答」按钮<br>8. 等待流程继续，最多重复 2 轮（断言 `.clarify-card` 最多出现 2 次后不再出现） | 出现带选项的澄清卡；点击选项 + 补充回答后继续；最多追问 2 轮后自动放行 |
| M2-05 | 明确需求不打扰 | 1. 导航至 `/chat` 页面<br>2. 在 `textarea.composer-input` 输入「对比 LangGraph 与 CrewAI 在 2026 年的社区活跃度与生产采用率」<br>3. 点击发送<br>4. 等待 `.agent-timeline` 出现 `plan` 或后续节点<br>5. 断言 `.clarify-card` 未出现（不触发澄清） | 不触发澄清（LLM 判定置信度足够），直接进入计划 |
| M2-06 | 用户取消不重连 | 1. 导航至 `/chat` 页面，输入深度研究问题并发送<br>2. 等待 SSE 流开始（`.agent-timeline` 有进度）<br>3. 点击 `.stop-button`（「⏹ 停止生成」）<br>4. 等待流终止<br>5. 拦截网络请求，统计后续 resume/state 请求次数<br>6. 断言无后续重连请求（count == 0）<br>7. 检查 `.message-item` 保留已生成部分 | 立即停止，无自动重连，会话标记 cancelled |

## 7. 回归范围

- 后端：`python -m pytest app/test -q` 全量，重点关注 `test_p2.py`（事件协议）、`test_events.py`、`test_stream_events.py`、`test_stream_contract.py`、`test_resume.py`、`test_hitl.py`、`test_p4.py`
- 前端：`npx vitest run` 全量，重点关注 `test_p6.test.ts`（事件 reducer）
- 协议不变量复验：任一流必以 completed/cancelled/error 终止；未知事件类型前端静默忽略（T2.1-06 与 T2.4-08 共同保障）
