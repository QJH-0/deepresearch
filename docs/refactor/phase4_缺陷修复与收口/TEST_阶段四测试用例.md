# 阶段四测试用例（缺陷修复与收口）

> 配套任务：R4.1 日志重复输出修复 / R4.2 E3–E5 前端缺陷修复 / R4.3 协议对齐与残留清理
> 优先级：P0 = 验收必过；P1 = 应尽量通过；P2 = 建议覆盖
> 特殊说明：本阶段为收口阶段，除各任务专属用例外，**全量回归 + 完整 E2E 冒烟本身就是核心验收项**（见第 6 节收口门禁）。

## 1. 测试文件规划

| 测试文件 | 覆盖任务 | 端 |
| --- | --- | --- |
| `app/test/test_logging_guard.py`（新增） | R4.1 | 后端 |
| `agent_front/test/test_chat_render.test.ts`（新增/扩展） | R4.2-E3 | 前端 |
| `agent_front/test/test_interrupt_card.test.ts`（新增/扩展） | R4.2-E4 | 前端 |
| `agent_front/test/test_cancel.test.ts`（新增/扩展） | R4.2-E5 | 前端 |
| `agent_front/e2e/research.spec.ts`（新增） | R4.2 全部 | E2E |
| `app/test/test_report_review.py`（新增/扩展） | R4.3 | 后端 |
| `agent_front/test/test_report_review_card.test.ts`（扩展） | R4.3 | 前端 |

## 2. T4.1 日志重复输出修复（R4.1）

### T4.1-01 重复调用零副作用【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 记录当前 root logger handlers 数量 N |
| 步骤 | 连续调用剩余的日志初始化路径（import app.app_main 两次/触发 research_logger 模块初始化/若 setup_logging 已删除则跳过——用「重复执行模块级初始化」替代） |
| 预期 | handlers 数量仍为 N（无增长） |

### T4.1-02 未标记 handler 检测告警【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 手动向 root logger 添加一个不带 `_deepresearch_root` 标记的 StreamHandler |
| 步骤 | 触发 app_main 的自检逻辑（重新加载或调用检测函数） |
| 预期 | 出现 warning 日志（含「重复挂载风险」提示） |

### T4.1-03 单条日志单份输出【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 捕获 root logger 输出（caplog 或自定义 handler） |
| 步骤 | `logger.info("test-single-line")` 一次 |
| 预期 | 捕获到该消息的 handler 中恰好 1 份（无倍数） |

### T4.1-04 每线程研究日志不受影响【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | `get_research_logger("t1")` 记录事件 |
| 预期 | `output/` 下该 thread 的 JSON 研究日志文件正常写入且结构不变（R4.1 不破坏 ResearchLogger） |

### T4.1-05 setup_logging 残留清零【P1】

| 项 | 内容 |
| --- | --- |
| 步骤 | `grep -rn "setup_logging" app/`（排除 conftest 的 mock 场景） |
| 预期 | 无业务代码调用残留（选择 A）或仅剩空操作兼容层（选择 B），与执行日志记录一致 |

## 3. T4.2 E3–E5 前端缺陷修复（R4.2）

> R4.2 为排查驱动任务，以下用例为「修复后的行为固化」——无论根因是什么，这些行为必须成立。

### T4.2-01 message.start 初始化消息对象【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | chat store 为某新 thread 分发 `run.started` 后紧接 `message.start`（message_id=m1, node=write） |
| 步骤 | 检查 store |
| 预期 | 该 thread 消息列表存在且含 m1 的消息对象（streaming 态）；后续 m1 的 delta 追加到该对象 content |

### T4.2-02 未知 thread 的 delta 不抛错【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 未收到 run.started/message.start，直接分发 `message.delta` |
| 步骤 | dispatch |
| 预期 | 不抛异常（容错跳过或惰性初始化——按修复实现断言） |

### T4.2-03 完整事件序列渲染回复【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 按真实时序 mock 后端事件流：run.started → agent.status×N → message.start → message.delta×5（文本片段） → run.completed |
| 步骤 | useEventStream 消费后挂载 ChatView（或直接断言 store） |
| 预期 | 消息 content 为 5 段文本按序拼接；streaming 态在 completed 后清除 |

### T4.2-04 run.error 显示错误态而非静默【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | mock 事件流以 `run.error(code=NoFinalOutput)` 结束 |
| 步骤 | 消费后检查 store |
| 预期 | chat error 态置位（UI 会渲染错误提示），无半截气泡残留 |

### T4.2-05 interrupt.raised 不触发递归更新【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 挂载 ChatView（含三张中断卡）；分发 `interrupt.raised(kind=plan_approval, payload={...大对象...})` |
| 步骤 | 等待 nextTick × 数次 |
| 预期 | 无 Vue 递归更新告警（捕获 console.warn/error 断言为空）；中断卡正常渲染 |

### T4.2-06 中断卡输入不直接改 store payload【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 挂载 PlanApprovalCard（payload 含 plan 数据） |
| 步骤 | 在修改反馈输入框中输入文字 |
| 预期 | 输入值存于组件本地 ref（store 中的 payload 不被双向绑定修改） |

### T4.2-07 取消后终态对齐【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 流进行中（mock 中间事件已分发）；调用取消并分发 `run.cancelled` |
| 步骤 | 检查 store |
| 预期 | streaming 态清除；消息保留已生成部分；会话标记 cancelled；reconnecting 态为 false |

### T4.2-08 abort 后不重连【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 用户点击停止（AbortError 场景） |
| 预期 | 不进入 R2.2 重连流程（state/resume 请求计数为 0） |

### T4.2-09 E2E 三场景（Playwright）【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | `agent_front/e2e/research.spec.ts`；后端 SSE 以 page.route mock 脚本化事件序列 |
| 场景 | E3：提问 → 回复气泡出现且文本完整；E4：审批卡出现 → 点批准 → 流程继续；E5：流式中点停止 → UI 终止且无后续事件消费 |
| 预期 | 三场景全部通过且无 console error |

## 4. T4.3 协议对齐与残留清理（R4.3）

### T4.3-01 reject 载荷校验通过【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | `ReportReviewResumePayload` 扩展后 |
| 步骤 | 分别构造 `{action:"approve"}`、`{action:"revise", sub_questions:[...]}`、`{action:"reject", feedback:"数据支撑不足"}` 验证 Pydantic |
| 预期 | 三种均通过；`{action:"invalid"}` 抛 ValidationError（422 路径） |

### T4.3-02 write 节点 reject 分支流转【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | report_review 中断后 resume reject 载荷（mock state） |
| 步骤 | write 节点恢复处理 |
| 预期 | 返回 `Command(goto="plan")`；否决理由进入 state（user_feedback 或既定字段）；plan 重走 |

### T4.3-03 前端否决按钮【P0】

| 项 | 内容 |
| --- | --- |
| 前置 | 挂载 ReportReviewCard |
| 步骤 | 点否决 → 输入理由 → 提交 |
| 预期 | emit 事件含 `action:"reject"` 与 feedback；未输入理由时提交按钮禁用或提示 |

### T4.3-04 协议三方一致【P0】

| 项 | 内容 |
| --- | --- |
| 步骤 | 脚本或人工核对：`EVENT_REGISTRY` 10 种事件 × `event-protocol.json` × 前端 `types/events.gen.ts` |
| 预期 | 事件类型集合与各事件 data 字段完全一致；不一致项清单为空 |

### T4.3-05 残留清理无悬空引用【P0】

| 项 | 内容 |
| --- | --- |
| 步骤 | `grep -rn "mult_agents.memory" app/ agent_front/src/`；全量 pytest |
| 预期 | 无业务代码引用已删除文件；全量测试全绿 |

### T4.3-06 reject/revise 语义区分【P1】

| 项 | 内容 |
| --- | --- |
| 前置 | 两轮 mock：revise 与 reject 各走一次完整恢复 |
| 预期 | revise：plan 保留原子问题 + 追加 sub_questions；reject：plan 收到否决理由上下文（断言注入字段） |

## 5. 浏览器自动化验证（阶段四收尾统一执行）

> 以下用例通过 Playwright 浏览器自动化执行，脚本存放于 `agent_front/e2e/phase4_acceptance.spec.ts`。
> 前置：后端服务运行在 `http://127.0.0.1:8000`，前端运行在 `http://localhost:5173`（或 8080）。
> 选择器参考：`textarea.composer-input`（输入框）、`button.send-btn`（发送）、`.stop-button`（停止）、`.plan-approval`（计划审批卡）、`.report-review`（报告审核卡）、`.message-item`（消息气泡）、`.agent-timeline`（进度时间线）、`.source-list`（来源列表）。
> 说明：M4-01 为日志验证，通过 `page.request` 调后端 API 触发研究并检查日志文件；其余均为浏览器 UI 自动化。

| 编号 | 场景 | 自动化步骤 | 预期 |
| --- | --- | --- | --- |
| M4-01 | 日志单份 | 1. 通过 `page.request` 调后端 API 发起一次完整研究<br>2. 等待研究完成后通过 `page.request` 读取 `output/logs/research/` 下对应 thread 的日志文件<br>3. 解析日志文件，统计同一条 INFO 消息出现次数<br>4. 断言每条 INFO 日志只出现一次<br>5. 同时通过 `page.request` 调 `GET /api/v1/admin/logs` 或等效接口检查控制台输出无重复 | 同一条 INFO 日志在控制台只出现一次；文件中只出现一次 |
| M4-02 | E3 实机 | 1. 导航至 `/chat` 页面<br>2. 在 `textarea.composer-input` 输入问题<br>3. 点击 `button.send-btn` 发送<br>4. 等待 `.message-item.assistant` 出现<br>5. 检查消息气泡内 `.markdown-render` 内容随 SSE 流式增长（分多次检查内容变化）<br>6. 等待 `.agent-timeline` 最终节点状态为 `done`<br>7. 断言消息内容完整且 `streaming` 态清除（无 loading spinner） | 气泡正常、token 流式、completed 后结束 |
| M4-03 | E4 实机 | 1. 导航至 `/chat` 页面，勾选 `.hitl-toggle input`（人工干预模式）<br>2. 输入问题并发送<br>3. 等待 `.plan-approval` 卡片出现<br>4. 检查卡片内含「批准」「修改」「否决」三个按钮（`button:has-text("批准")` 等）<br>5. 点击「批准」按钮<br>6. 等待流程继续，断言 `.plan-approval` 消失<br>7. 检查后续 `.message-item.assistant` 正常渲染 | 卡片正常、无冻结、三按钮可交互 |
| M4-04 | E5 实机 | 1. 导航至 `/chat` 页面，输入深度研究问题并发送<br>2. 等待 SSE 流开始（`.agent-timeline` 有进度）<br>3. 点击 `.stop-button`（「⏹ 停止生成」）<br>4. 等待 `.message-item` streaming 态清除<br>5. 检查 `.agent-timeline` 最后节点状态不为 `running`<br>6. 断言无后续 SSE 事件消费（通过 `page.route` 拦截统计后续请求数为 0） | 立即停止、状态对齐 |
| M4-05 | reject 实机 | 1. 导航至 `/chat` 页面，勾选人工干预模式<br>2. 输入问题并发送，等待 `.plan-approval` 出现<br>3. 点击「否决」按钮（`button:has-text("否决")`）<br>4. 等待流程重走计划<br>5. 再次等待 `.plan-approval` 或后续流程节点出现<br>6. 最终检查 `.message-item.assistant` 产出新报告<br>7. 断言新报告内容与第一次不同（否决理由已注入） | 系统带否决理由重走计划，最终产出新报告 |
| M4-06 | 检索失败兜底（若 R4.2 涉及） | 1. 通过 `page.route` 拦截搜索相关 API 请求，mock 返回空结果或错误<br>2. 导航至 `/chat` 页面<br>3. 在 `textarea.composer-input` 输入问题并发送<br>4. 等待响应完成（`.message-item.assistant` 出现）<br>5. 检查消息内容含「证据不足」或等效说明文本<br>6. 断言无报错弹窗或挂起状态（`.message-item` 无 error 态） | 报告产出「证据不足说明」而非空回复/报错挂起 |

## 6. 收口门禁（整个重构体系完成标准）

阶段四完成时，执行**最终收口回归**并将结果记录到 `docs/refactor/执行日志.md`：

| 门禁 | 标准 |
| --- | --- |
| 后端全量 | `python -m pytest app/test -q` 全绿 |
| 前端全量 | `cd agent_front && npx vitest run` 全绿 |
| E2E | E1–E5 全部通过（Playwright 浏览器自动化执行） |
| 完整冒烟 | 一次完整研究闭环：提问 → （澄清）→ 计划审批 → 检索 → 报告流式输出（含 thinking）→ 报告评审（采纳/否决各一次）→ 导出 MD/PDF |
| 断线恢复 | M2-02 场景通过 |
| 文档向量化 | 上传 → indexed → 检索命中（M3-06） |
| 双实例 | M3-01 取消广播通过（有条件时） |
| 一致性 | T4.3-04 协议三方一致清单为空 |
| 简历口径对账 | 对照 `docs/项目功能总结_简历用.md` 第 7 节 13 项清单逐项打勾，未完成项列出原因 |
