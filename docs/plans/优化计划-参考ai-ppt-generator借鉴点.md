# DeepResearch 优化计划 — 基于 ai-ppt-generator 借鉴点

> 来源：`docs/dev/参考项目借鉴-ai-ppt-generator-分析.md` 的 A / B / C 三类借鉴点
> 状态：**待确认**（按 AGENTS.md「大改动前置输出模板」，确认后方可执行）
> 编写日期：2026-09-17

---

## 一、目标（Goal）

按「先补短板、再补机制、最后动数据」的顺序，把 ai-ppt-generator 的三类工程约束习惯落到本项目：

1. **模型输出受 schema 约束** —— 消除 `_extract_json_block` 正则抠 JSON + 静默 fallback 的路径；
2. **产物可被验证** —— 报告导出前有 error/warning 分级门禁，导出后有产物级回读校验；
3. **契约单一真源 + 对拍断言** —— 事件协议从「手动生成」升级为「自动生成 + 自动断言」。

**完成标准**：六个批次全部通过各自验收项；`pytest` 全绿；导出链路具备阻断能力；事件协议漂移可被测试捕获。

**验证方式**：每批次独立跑 `app/test/` 相关子集 + 手工验证一条真实用户路径（见各批次「验收」）。

---

## 二、阻塞问题（3 个）

### 阻塞问题 1：`create_agent(response_format=...)` 会改变流式语义

已核实环境：`langchain 1.0.5` / `langgraph 1.0.3`，`create_agent` **支持** `response_format` 参数（默认 `None`）。

但结构化输出在 langchain 1.x 中是通过**工具调用**产生的，不是正文文本。而 `app/mult_agents/nodes/_parsing.py:72-125` 的 `_invoke_json_agent` 当前依赖 `stream_mode="messages"` 累加正文 token 来拼 JSON，并把它 `emit(node, content)` 推给前端。改造后：

- 正文 token 流将不再是 JSON 文本，`content` 可能为空或变成工具调用参数片段；
- `writer({"node": node, "message": f"推理完成: {preview}"})` 的预览内容失去意义；
- `_parsing.py:78-81` 的注释明确写着「只推 reasoning 与进度，不把正文推成 message.delta」——该设计意图需在新方案中保持。

**默认建议**：`_invoke_json_agent` 增加 `schema` 参数，`response_format` 只用于**决策类节点**（intent / plan / analyze / reflect），这些节点本就不需要展示正文；`write` 与 `direct_answer` 保持现状（它们产出的是用户可见正文，不应结构化）。先在 `intent` 单点试点，验证 `ChatTongyi` + `response_format` 组合可用后再推广。

### 阻塞问题 2：A3 涉及表结构变更，必须先走 DDL 确认关卡

`input_signature` / `expected_revision` 需要给业务表加字段。按 AGENTS.md「数据变更与 DDL 确认关卡」，**未经书面方案确认不得执行任何 DDL**。故 A3 单独拆为批次 6，本计划只登记范围，不产出 DDL 草案——待前五批完成后再单独出方案。

### 阻塞问题 3：C3 安全自查的现状未审计

`越权返回 404 而非 403`、`错误信息脱敏`、`恒定时间比对` 三项，本项目可能已部分实现（`app/backend/auth/deps.py` 与各 router 未逐一核对）。若已实现则不应重复改动。

**默认建议**：批次 1 中先做**只读审计**，产出「已满足 / 缺失」清单，仅对缺失项动手。

---

## 三、假设（Assumptions）

| 维度 | 假设 |
| --- | --- |
| 运行环境 | 测试与验证统一用 conda `llmdev`（`D:\develop_tools\miniconda3\envs\llmdev\python.exe`），managed 3.13 无 pytest |
| 依赖 | `pypdf` 在 `llmdev` 中**已可用**（已核实），B2 无需新增依赖；`requirements.txt` 未列 `pypdf`，需同步补登 |
| 模型能力 | `ChatTongyi` 支持工具调用，因此支持 `response_format`；**此假设需在批次 2 的 intent 试点中证伪/证实** |
| 失败行为 | 结构化输出失败时应**显式失败并记录**，不再静默返回 fallback；但 HITL / 前端不得因单节点失败而整轮崩溃，需保留受控降级 |
| 边界范围 | 不改变图的拓扑、State 契约、SSE 事件协议字段（仅新增可选字段）；不改前端交互流程 |
| 需求范围 | 本计划只做 A/B/C 借鉴点，不含无关重构；发现问题只登记不顺手修 |
| 测试方案 | 批次 1/3/5 需补单测；批次 2 需在 Fake LLM 下可测；批次 4 需并发行为测试 |

---

## 四、计划（Plan）

### 批次 1 — 工程加固（低风险，可立即执行）

| 项 | 借鉴点 | 改动文件 | 要点 |
| --- | --- | --- | --- |
| 1.1 上传真实类型校验 | A2 | `app/backend/router/document_router.py`（+ 新增 `app/backend/service/upload_guard.py`） | 扩展名白名单之后加 magic bytes 校验；docx 进一步校验 zip 内 `word/document.xml` 存在；伪装文件返回 400 |
| 1.2 LLM 显式超时与重试 | A4 | `app/mult_agents/models.py:25-46` | `ChatTongyi` 与 `ChatOpenAI` 统一注入 `timeout` / `max_retries`；值走 `BusinessSettings` 新增字段而非硬编码 |
| 1.3 配置项落位 | A4/B3 前置 | `app/backend/config/settings.py`（`BusinessSettings`）、`app/mult_agents/config.py`（`AppConfig` 兼容层） | 新增 `llm_timeout_seconds`、`llm_max_retries`；保持 `AppConfig` 字段访问方式不变（沿用 P0 兼容层模式）。`max_llm_concurrency` 移至批次 4.1 随消费方一起加，避免留下无人读取的死配置 |
| 1.4 安全现状审计 | C3 | 只读，产出清单 | 越权 404、错误脱敏、恒定时间比对三项逐条核对；仅对缺失项提改动 |
| 1.5 JSONB 更新写法审计 | C4 | 只读，产出清单 | 排查所有 JSONB 字段是否存在「就地改 dict 不赋新值」的静默丢更新 |

**验收**：1.1 用一个改扩展名的伪装文件验证被拒；1.2 单测断言模型实例带 timeout；1.4/1.5 产出清单并经你确认范围。

**开发顺序**：1.3 → 1.2 → 1.1 →（1.4 / 1.5 只读，可并行）

---

### 批次 2 — 结构化输出（收益最大，需先试点）

| 项 | 借鉴点 | 改动文件 | 要点 |
| --- | --- | --- | --- |
| 2.1 intent 试点 | A1 | `app/mult_agents/nodes/intent.py`、`nodes/_parsing.py` | `_invoke_json_agent` 增 `schema: type[BaseModel] | None`；试点只覆盖 intent，验证 `response_format` 在 `ChatTongyi` 上可用 |
| 2.2 决策节点推广 | A1 | `nodes/plan.py`、`nodes/analyze.py`、`nodes/reflect.py`(在 analyze.py 内) | 各自定义 Pydantic 模型（带字段级长度约束），派生字段由服务端补齐 |
| 2.3 失败路径改造 | A1 | `nodes/_parsing.py` | 结构化失败改为显式异常 + 日志；`_extract_json_block` 仅保留为非关键路径兜底 |
| 2.4 流式语义修复 | 阻塞问题 1 | `nodes/_parsing.py` | 确保改造后 reasoning 流式与「不推正文」的既有设计意图不被破坏 |

**不做**：`write`、`direct_answer` 保持现状（产出用户可见正文，不结构化）。

**验收**：intent 试点跑通一次真实调用；`app/test/test_p1.py`、`test_p2.py`、`test_stream_events.py` 全绿；Fake LLM 下新增单测覆盖「结构化成功 / 结构化失败」两条路径。

**回滚**：`schema=None` 时完全走旧逻辑，可按节点粒度回退。

---

### 批次 3 — 导出质量门禁与回读验证

| 项 | 借鉴点 | 改动文件 | 要点 |
| --- | --- | --- | --- |
| 3.1 报告质量检查器 | B1 | 新增 `app/backend/service/report_check.py` | 定义 `ReportIssue(severity, section, code, message)`；按 `severity + section + code + message` 去重 |
| 3.2 分级规则 | B1 | 同上 | **error**：引用编号悬空、无引用支撑的结论段落；**warning**：篇幅过短、单一信源占比过高、图表缺标题 |
| 3.3 导出后回读校验 | B2 | `app/backend/service/pdf_export_service.py` | 用 `pypdf` 校验页数 > 0、可解析、关键章节标题确实存在；不通过则降级返回 Markdown 并在响应标注原因 |
| 3.4 导出接口接门禁 | B1 | `app/backend/router/research_router.py:481-518` | error 存在则阻断（409/422），warning 放行但随响应返回；沿用现有 Markdown 降级分支 |
| 3.5 依赖登记 | — | `requirements.txt` | 补登 `pypdf`（已在 llmdev 中可用，但未声明） |

**验收**：构造「引用悬空」报告验证被阻断；构造正常报告验证 PDF 可下载且回读校验通过；`test_pdf_export.py` 全绿并新增门禁用例。

---

### 批次 4 — 并发与节奏

| 项 | 借鉴点 | 改动文件 | 要点 |
| --- | --- | --- | --- |
| 4.1 全局并发上限 | B3 | `app/mult_agents/models.py` 或新增 `app/mult_agents/concurrency.py` | 新增配置项 `max_llm_concurrency` 并落地模块级 `asyncio.Semaphore`，所有 LLM/检索调用前 `async with` |
| 4.2 重试与重跑同路径 | B3 | `app/backend/service/research_service.py`、`task_registry.py` | 「单点重试」与「全量重跑」共用同一执行函数，避免两套代码分叉 |
| 4.3 任务看门狗 | A4 延伸 | `app/backend/service/task_registry.py` | 运行中任务加超时看门狗，超时置终态并发 `run.failed`，避免前端永久 loading |
| 4.4 确定性章节配额 | B4 | `app/mult_agents/nodes/plan.py`、`prompts.py` | plan 阶段按位置确定性下发「本章目标字数区间 / 是否需图表 / 是否需对比表」，写进各章 prompt |

**验收**：并发行为测试断言并发数不超过上限；看门狗用例断言超时后状态为终态；章节配额在真实一次运行中可见于各章 prompt。

**注意**：4.1 若与 LangGraph 双出边并行叠加，需确认信号量不会造成死锁（`plan → web_search ∥ local_rag` 两条边同时抢信号量）。

---

### 批次 5 — 契约对拍与回归基线

| 项 | 借鉴点 | 改动文件 | 要点 |
| --- | --- | --- | --- |
| 5.1 事件协议对拍断言 | B5 | 新增 `app/test/test_event_protocol_parity.py` | 断言后端 `EVENT_REGISTRY` 的 Pydantic schema ↔ 前端 `agent_front/src/types/events.gen.ts` 字段集合相等 |
| 5.2 生成动作自动化 | B5 | `scripts/export_event_protocol.py` + 测试或 pre-commit | 把手动生成改为可校验：生成结果与仓库文件不一致即失败 |
| 5.3 测试分层 | C1 | `app/test/conftest.py` + 新增 Fake LLM fixture | Fake LLM 单测进 CI（零成本），真实 LLM 评测（`eval_metrics.py`）按需手动跑 |
| 5.4 固定语料回归基线 | C2 | `app/test/` + 新增固定问题集 | 覆盖 direct / 需澄清 / 多子问题 / 本地知识库为主 四类；指标落盘为可比对历史 JSON，设阈值告警 |

**验收**：故意改一个事件字段名，5.1 必须失败；5.3 的 Fake LLM 单测在不配 API Key 时可跑通；5.4 产出一份基线 JSON。

**注意**：5.4 的固定问题集若走真实 LLM，会产生额度消耗，需你确认是否纳入常规流程。

---

### 批次 6 — 数据层加固（需单独 DDL 确认）

| 项 | 借鉴点 | 范围 |
| --- | --- | --- |
| 6.1 输入指纹 | A3 | 报告/任务表新增 `input_signature`（问题 + 检索参数 + 文档版本集合的 hash），生成前比对，不一致则标记旧产物 `stale` |
| 6.2 条件更新 | A3 | 重试类接口补 `expected_revision` 条件更新，丢弃迟到写入 |
| 6.3 幂等键 | A3 | 为可重入的生成产物设计幂等键（参照 `(project_id, outline_page_id)` 唯一键思路） |

**本批次只登记范围，不产出 DDL 草案。** 待前五批完成后，单独输出含「业务语义 / 现状 / 拟议模型 / DDL 草案（待确认）/ 迁移与回滚 / 需决策的业务规则」的书面方案，经你确认后再执行。

---

## 五、执行顺序与依赖

```
批次 1（工程加固）──────────► 可立即开始，无依赖
      │
      ├─ 1.3 配置项落位 ──► 批次 2（结构化输出）、批次 4（并发）共用
      │
批次 2（结构化输出）────────► 依赖 1.3；含阻塞问题 1 的试点验证
      │
批次 3（导出门禁）──────────► 独立，可与批次 2 并行
      │
批次 4（并发与节奏）────────► 依赖 1.3、批次 2（4.4 依赖 plan 节点改造）
      │
批次 5（契约与回归）────────► 独立，可与批次 3/4 并行
      │
批次 6（数据层）────────────► 依赖批次 2（产物稳定后才有指纹意义）；需单独 DDL 确认
```

## 六、风险与回滚

| 风险 | 缓解 |
| --- | --- |
| `response_format` 在 `ChatTongyi` 上不可用 | 批次 2 单点试点先行，失败则退回「模型直接 `with_structured_output`」或维持现状，不推广 |
| 结构化输出改变了前端可见的推理进度 | 2.4 专门守住「不推正文」设计意图；试点时人工核对前端表现 |
| 信号量引入死锁 | 4.1 先只包 LLM 调用不包整节点；用并发行为测试兜底 |
| 导出门禁误伤正常报告 | 门禁规则先只上 warning 观察，确认无误判后再升为 error |
| 批次 5 回归集消耗额度 | 5.4 默认只跑 Fake LLM 版本；真实 LLM 版本手动触发 |

**回滚**：批次 1/3/4/5 均为代码级改动，`git revert` 即可；批次 2 有 `schema=None` 的节点级开关；批次 6 需回滚脚本（届时随 DDL 方案一并提供）。

---

## 七、待你决策的事项

1. **起始批次**：批次 1 无依赖且风险最低，建议从这里开始。
2. **批次 5.4 是否纳入常规流程**（涉及真实 LLM 额度消耗）。
3. **批次 6 是否现在就要**（要动表结构，需单独走 DDL 确认，建议押后）。

---

## 八、批次 1 执行记录（2026-09-17）

已按确认结果执行：起始批次 = 批次 1；批次 5.4 只跑 Fake LLM 版本；批次 6 押后。

### 已完成的代码改动

| 项 | 文件 | 内容 |
| --- | --- | --- |
| 1.3 配置项落位 | `app/backend/config/settings.py`、`app/mult_agents/config.py` | 新增 `llm_timeout_seconds`（默认 60.0）、`llm_max_retries`（默认 2），含 `_env_float` 环境变量覆盖 |
| 1.2 超时与重试注入 | `app/mult_agents/models.py` | `build_agent` 新增必填关键字参数 `timeout` / `max_retries`；`ChatOpenAI` 直传，`ChatTongyi` 经 `model_kwargs={"request_timeout": ...}` 透传 |
| 1.1 上传类型校验 | 新增 `app/backend/service/upload_guard.py`、`app/backend/router/document_router.py` | 按魔数校验 PDF / OOXML（含 `word/document.xml` 内部结构）/ OLE2 / 文本类；不符返回 400 |
| 测试 | `app/test/test_p1.py`、`app/test/test_fix_regressions.py` | 新增 2 个模型工厂用例、8 个上传校验用例；修正 2 处测试替身签名 |

### 执行中发现的既有事实（影响后续批次）

1. **`ChatTongyi.max_retries` 默认值为 10** —— 失败时会长时间重试，是请求长尾的真实来源。本次已显式收窄为 2。
2. **`ChatTongyi` 无 `timeout` 字段** —— 只能经 `model_kwargs` 的 `request_timeout` 透传给 dashscope（已实测 `_default_params` 中生效）。计划中原设想的「统一注入 timeout」在实现层是两条不同路径。
3. **`test_fix_regressions.py` 整文件运行会挂起**（卡在 `TestPublishMessagesHonoursReturnValue::test_mq_connect_failure_clears_producer`，约 83% 处），需 RabbitMQ 环境。属**既有环境依赖问题**，与本次改动无关；本次改用按类运行验证。
4. **`build_agent` 会写 `os.environ["DASHSCOPE_API_KEY"]`** —— 测试若传入非空 `api_key` 会污染进程环境，导致 `test_events.py::test_config_loading_from_env_and_json` 失败。新增用例一律传空 `api_key`。

### 审计结论（只读，未改代码）

**C3 安全现状**

| 检查项 | 结论 | 证据 |
| --- | --- | --- |
| 恒定时间比对 | **已满足** | `auth/security.py:21-22,60-62` 用 `hmac.compare_digest`，用户不存在时以 `_DUMMY_PASSWORD` 做等长比对 |
| 全局异常脱敏 | **已满足** | `app_main.py:341-355` 未捕获异常只回通用文案 + trace_id，明确不携带 `str(exc)` |
| 越权返回 404 而非 403（文档删除） | **已满足** | `document_service.delete_document` / `delete_documents_batch` 按 `user_id` 过滤，未命中返回「文档不存在」/`deleted=0` |
| 越权返回 404 而非 403（会话读取） | **缺失（IDOR）** | 见下方「待决策风险」 |
| 认证 401 错误信息脱敏 | **部分缺失** | `auth/deps.py:43` 回显 `str(exc)`，会把 PyJWT 的具体失败原因（签名失败 / 已过期）返回给调用方 |

**C4 JSONB 更新写法**：**不适用**。本项目用原生 psycopg + 显式 SQL，无 ORM 脏检查机制。两处 JSONB 列（`document_chunks.metadata`、`chunk_sync_messages.payload`）的更新路径均为显式语句，其中 `metadata = metadata || %s`（`postgres_client.py:328`）是**服务端合并**，比「读→改→整体赋值」更安全，不存在并发丢失更新。`document_service.py:460` 的就地改 dict 发生在入库前的临时 LangChain Document 上，无持久化影响。

### 待决策风险（未修，需你确认后再动）

**会话级读取接口缺少归属校验（IDOR）**：`research_router.py` 中 `get_state`（:339-359）、`get_interrupt`（:375-389）、`get_history`（:392）、`export_markdown`（:450-476）、`export_pdf`（:481+）均声明了 `current_user` 但**未把 `user_id` 传入服务层**，服务层按 `thread_id` 直查。任何已登录用户只要拿到（或猜到）`thread_id`，即可读取他人会话的状态、历史与报告导出。

对照：同文件的 `list_threads`、`delete_thread` 已正确按 `user_id` 过滤。

影响面：`thread_id` 为 UUID，需先泄露 ID 才可利用，故非直接高危，但属明确的水平越权，且报告导出接口会**返回完整报告正文**。

### 验证结果

| 命令 | 结果 |
| --- | --- |
| `pytest app/test/test_p1.py app/test/test_state.py app/test/test_auth.py app/test/test_admin_config.py app/test/test_events.py app/test/test_stream_events.py app/test/test_thinking_stream.py -q` | **102 passed** |
| `pytest app/test/test_fix_regressions.py::TestUploadTypeGuard TestUploadSizeGuard TestAgentBuilderConsolidation -q` | **16 passed** |
| `pytest app/test/test_fix_regressions.py -q`（整文件） | **挂起**，卡在 MQ 连接用例（既有环境依赖，非本次改动引入） |

**未验证部分**：未用真实伪装文件走一遍 HTTP 上传路径（仅覆盖 `verify_upload` 单元层）；未跑全量 `app/test/`（含需 DB / Milvus / RabbitMQ 的用例）。

---

## 九、批次 2 执行记录（第一步：intent 试点）

### 阻塞问题 1 的实测结论：`response_format` 方案被证伪

| 尝试 | 结果 |
| --- | --- |
| `create_agent(response_format=IntentDecision)` | **失败** —— langchain 1.x 走强制 `tool_choice`（指定 function），DashScope 只接受 `none`/`auto`，报 `InvalidParameter` |
| `with_structured_output(schema, method="json_mode")` | **失败** —— `ChatTongyi.with_structured_output` 不接受 `method` 参数，`Received unsupported arguments` |
| `with_structured_output(schema)` | 可用，但内部固定走 `bind_tools` + `PydanticToolsParser`，**不透出流式增量**，深度思考节点会失去 reasoning 流式 |
| `bind_tools([schema])` + `astream` + 逐块合并 | **采用** —— 既拿到受 schema 约束的工具调用结果，又保留 reasoning 流式 |

### 关键坑：提示词里的输出格式要求会与 schema 约束打架

首轮实现后真实调用返回 `agent_messages=0`（走了规则引擎降级）。诊断发现：`intent_router` 原文含「你必须只输出 JSON，格式固定为 `{"route":...}`」，模型**照着提示词回了一段文本 JSON**，而不是发起工具调用 → `tool_calls` 为空 → 结构化路径判定失败。

**结论：结构化节点的提示词不得规定输出格式**，只描述判断标准；格式由 schema 承担。已加入回归测试锁住该约束。

### 已完成的代码改动

| 项 | 文件 | 内容 |
| --- | --- | --- |
| 输出 schema | 新增 `app/mult_agents/output_schemas.py` | `IntentDecision`（`route` 为 `Literal` 枚举、`reason` 带 `max_length`） |
| 结构化执行体 | `app/mult_agents/models.py` | 抽出 `_build_llm` 供两条路径共用；新增 `StructuredAgent` 与 `build_structured_agent`（`bind_tools` 绑定 schema） |
| 结构化调用 | `app/mult_agents/nodes/_parsing.py` | 新增 `StructuredOutputError` 与 `_invoke_structured_agent`（逐块合并 + schema 校验 + reasoning 透传） |
| 节点改造 | `app/mult_agents/nodes/intent.py` | 改走结构化路径；失败时显式告警并回退规则引擎 |
| 提示词 | `app/mult_agents/prompts.py` | `intent_router` 去掉 JSON 格式要求 |
| 测试隔离 | `app/test/conftest.py` | 新增 autouse fixture 隔离 `DASHSCOPE_API_KEY`（`_build_llm` 会写 `os.environ`，泄漏会让后续读取 `.env` 的用例失败） |
| 测试 | 新增 `app/test/test_structured_output.py` | 7 例：校验成功 / 分块参数合并 / 无 tool_calls 失败 / 枚举越界失败 / 构建器绑定 / 装配 / 提示词无格式要求 |

### 真实调用验证

用 `qwen-plus` 对 `intent_node` 跑三条真实输入，均走结构化路径（`agent_messages=2`，无降级告警）：

| 输入 | 路由 |
| --- | --- |
| 你好，你是谁？ | direct |
| 帮我调研 2026 年国内 AI Agent 市场格局 | multiagent |
| 今天天气如何 | direct |

### 验证结果

| 命令 | 结果 |
| --- | --- |
| `pytest test_structured_output + test_p1 + test_thinking_stream + test_events + TestNodesDoNotPolluteDraft -q` | **49 passed** |
| 批次 1 + 批次 2 相关全量（20 个文件/类） | **226 passed** |

**未做**：`plan` / `analyze` / `reflect` 的推广（本批次第二步）；`web_search` / `local_rag` / `deep_dive` 的证据结构未结构化（结构复杂且非决策节点，暂不纳入）。

---

## 十、模型切换与结构化输出方案修订（Qwen3.7/3.8 + JSON Schema）

批次 2 第一步落地后，确认了 Qwen3.7/3.8 系列原生支持 JSON Schema 结构化输出。这推翻了此前「DashScope 不支持 response_format」的结论——那个结论只在旧型号上成立。本节记录修订后的方案。

### 10.1 实测确认的事实

| 事实 | 证据 |
| --- | --- |
| **JSON Schema 模式仅支持 5 个系列**：Qwen3.7-Plus / Qwen3.7-Flash / Qwen3.7-Max / Qwen3.8-Flash / Qwen3.8-Max | 百炼《千问结构化输出》文档 |
| 上述 5 个型号的调用 ID 全部可用，`response_format={"type":"json_schema", ..., "strict":true}` 被接受 | 5 个型号逐一真实调用，非流式与流式均返回可校验的 JSON |
| **旧型号 `qwen-plus` 不支持 JSON Schema 模式** | 同一文档；且旧结论中的 `create_agent(response_format=...)` 报 `InvalidParameter` 正是因为它走的是 JSON Schema 路径 |
| **原生 SDK 通道（`ChatTongyi`）不支持 Qwen3.7/3.8 系列** | `ChatTongyi(model="qwen3.7-flash")` 真实调用直接报 ValueError |
| Qwen3.7-Flash 等型号**默认开启思考**，不显式传参会付出成倍延迟 | 同一问题：默认 7.5s / 显式关闭 1.0s |
| 这些型号在兼容通道下**不透出 reasoning 文本** | 流式与非流式、`enable_thinking` 开与关、加 `incremental_output` 四种组合，`reasoning_content` 长度均为 0 |

### 10.2 方案修订：从工具调用变通方案改为原生 JSON Schema

| | 批次 2 第一步（旧） | 本次修订（新） |
| --- | --- | --- |
| 约束机制 | `bind_tools([schema])`，模型以工具调用产出 | `bind(response_format={"type":"json_schema","strict":true})` |
| 结果位置 | `tool_calls[0]["args"]` | 正文 `content`（即 JSON 字符串） |
| 约束强度 | 模型"应该"调工具，不保证 | provider 级强制，`strict: true` |
| 适用型号 | 任意支持工具调用的型号 | **仅上述 5 个系列** |
| 失败模式 | 模型回文本 JSON → `tool_calls` 为空 → 静默降级 | provider 保证结构；解析失败即说明约束失效，直接暴露 |

`_invoke_structured_agent` 相应简化：不再逐块合并 `AIMessageChunk` 取 `tool_calls`，改为累加正文后 `model_validate_json`。**不做代码围栏剥离、不做正则截取**——provider 已保证结构，宽容解析只会掩盖"约束失效"。

### 10.3 配置变更

| 配置项 | 变更 |
| --- | --- |
| `model`（默认模型） | `qwen-plus` → `qwen3.8-max`（`.env` 的 `MODEL` 与 `config.json` 同步） |
| `node_models`（按节点分档） | **新启用**（见下） |
| `memory_extract_model` | `qwen-turbo` → `qwen3.7-flash` |
| `summary_model` | `qwen-turbo` → `qwen3.7-flash` |
| `title_model` | **新增**，`qwen3.7-flash` |
| `scorer_model` | **新增**，`qwen3.7-flash`（证据评分高频调用，独立于主模型控成本） |

分档策略：长文产出与深度分析用 Max，检索过滤与判定用 Flash，补搜计划用 Plus。

```json
{
  "write": "qwen3.8-max", "deep_dive": "qwen3.8-max", "analyze": "qwen3.8-max", "plan": "qwen3.8-max",
  "reflect": "qwen3.7-plus",
  "web_search": "qwen3.7-flash", "local_rag": "qwen3.7-flash", "intent_router": "qwen3.7-flash",
  "direct_answer": "qwen3.8-flash", "clarify": "qwen3.7-flash"
}
```

### 10.4 顺带修掉的既有缺陷

1. **`node_models` 是断链特性** —— `models.build_agents` 一直在读 `getattr(config, "node_models", None)`，文档注释也写了用法，但配置层从未定义该字段，取值恒为 `None`。已补齐 `BusinessSettings.node_models` 与 `AppConfig.node_models` 并打通映射。
2. **`clarifier` 硬编码 `qwen-turbo`** —— 改为走 `node_models` 的 `clarify` 键，与其他节点一致可配。
3. **辅助链路锁死在旧型号** —— 标题/摘要/记忆抽取三处仍用原生 SDK + `qwen-turbo`。由于原生 SDK 不支持新系列，已统一迁移到 `build_aux_llm`（与主链路共用兼容通道客户端与超时/重试策略），`ChatTongyi` 依赖从生产代码中完全移除。
4. **证据评分器会因型号切换静默降级**（本次改动引入、已修）—— `_fallbacks._get_scorer_llm` 原用 `ChatTongyi(model=get_business_settings().model)`，默认模型切到 `qwen3.8-max` 后原生通道不支持该型号，构造失败会被 `except` 吞掉并置 `_scorer_llm_unavailable`，证据评分静默退回纯先验。已迁移到 `build_aux_llm` 并新增独立的 `scorer_model`（默认 `qwen3.7-flash`）—— 该 LLM 在 `deep_dive` 中高频调用，不应跟随主模型承担 Max 档成本。

### 10.5 新增的硬约束（已写入测试）

- 默认模型必须支持 `json_schema`：`test_default_model_supports_json_schema`
- `node_models` 里给结构化节点配的型号也必须支持：`test_structured_nodes_configured_with_schema_capable_models`
- 配错型号**启动即失败**：`build_structured_agent` 对不支持的型号抛 `ValueError` 并列出可选型号，而不是留到运行期报 schema 校验错误
- **结构化输出下禁止设置 `max_tokens`**（会截断 JSON 产生非法输出）—— 现状核查：主链路未设置，符合要求

### 10.6 待办与风险

| 项 | 说明 |
| --- | --- |
| **深度思考流式失效** | 新系列在兼容通道下不透出 `reasoning_content`，`thinking` 事件将不再产生。代码对空 reasoning 是安全的（仅在有内容时推送），但用户侧"研究过程"会变安静。需另找通道或改用其他方式呈现进度 |
| `qwen3.8-plus` 未在文档的 JSON Schema 支持列表中 | 若要用 Plus 档请选 `qwen3.7-plus`；`build_structured_agent` 会在配错时直接报错 |
| 成本 | `qwen3.8-max` 输入 12 元 / 输出 36 元每百万 tokens，明显高于 `qwen-plus`。分档策略即为此而设；如成本敏感可把 `write`/`deep_dive` 降到 Plus 档 |

### 10.7 验证结果

| 命令 | 结果 |
| --- | --- |
| 21 个测试文件（含 p1–p5、stream、thread、structured、summary、memory、evidence、pdf 等） | **326 passed** |
| 真实调用：四个决策节点（按分档型号） | intent→`qwen3.7-flash` 判 multiagent；plan→`qwen3.8-max` 出 6 节大纲；analyze→`qwen3.8-max`；reflect→`qwen3.7-plus` 出 4 条补搜。全部走结构化路径，无降级告警 |
| 真实调用：标题生成 | `qwen3.7-flash` 正常产出 |
| 真实调用：证据评分器构造 | `_get_scorer_llm` 返回可用实例，`_scorer_llm_unavailable=False`，无静默降级 |
| 真实调用：5 个型号 × JSON Schema（非流式 + 流式） | 全部返回可校验 JSON |

**未验证**：未跑完整端到端研究流程（需 Milvus / RabbitMQ / PG 全栈中间件）；未验证 `web_search` / `local_rag` / `deep_dive` 在新型号下的检索与证据抽取质量（这些节点仍走自由文本 JSON 解析）。
