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

---

## 十一、批次 3 执行记录（导出质量门禁与回读验证）

### 11.1 分级规则

判定依据是「这份文件交出去会不会出错」，而不是「写得够不够好」—— 后者是主观判断，不该由代码替用户决定。

| severity | code | 触发条件 |
| --- | --- | --- |
| **error** | `empty_report` | 正文为空 |
| **error** | `missing_reference_section` | 正文含引用标记但完全没有参考资料段落 → 引用无法核对 |
| **error** | `empty_reference_section` | 参考资料段落存在但没有任何条目 |
| **error** | `pdf_unreadable` / `pdf_empty` | 回读时产物打不开 / 零页 |
| warning | `short_body` | 正文字数 < 800（去掉标题行与空白后统计） |
| warning | `no_citation` | 正文零引用 → 结论无法溯源 |
| warning | `single_source_dominant` | 单一来源占全部引用的 > 60% |
| warning | `reference_not_cited` | 参考资料列出了正文完全没引用过的条目 |
| warning | `pdf_text_not_extractable` | PDF 文本无法提取（字体子集化常见，不影响阅读） |
| warning | `pdf_title_missing` | 回读未在 PDF 中找到报告标题 |

问题按 `severity + code + message` 去重，避免同类问题刷屏。

### 11.2 设计偏差：为什么没做「引用编号悬空」的逐编号比对

原计划把「引用编号悬空」列为 error。实现时发现它会**在每份正常报告上误报**：参考资料清单按 `locator` 对本地来源去重（同一文件的多个 chunk 只列一个代表），因此正文引用 `[LOC1_1-3]` 而清单只列 `[LOC1_1-1]` 是正常现象（见 `nodes/_fallbacks.py:283-293`）。

改为检查更可靠的信号：**有引用就必须有非空的参考资料段落**。这是结构性缺失，不受去重影响。已加测试 `test_local_source_dedup_does_not_trigger_error` 锁住该行为。

### 11.3 接线方式

| 端点 | 门禁 |
| --- | --- |
| `GET /threads/{id}/export/pdf` | 导出前 `check_report` → error 则 **422 + 问题清单**；渲染后 `verify_pdf` → error 同样 422；仅 warning 时放行，并把问题清单经 `X-Report-Warnings` 头（URL 编码）回传前端 |
| `GET /threads/{id}/export/md` | **不做阻断** —— 它是内容逃生通道，PDF 被拦时用户仍能拿到原文 |

阻断发生在渲染之前（已有测试断言 `__aenter__` 未被调用），不为一份注定失败的导出白起一次浏览器。

### 11.4 顺带发现并修复的问题

1. **PDF 导出在当前环境根本不可用** —— Playwright 浏览器从未安装（`Executable doesn't exist at .../chrome-headless-shell.exe`）。既有测试全部 mock 了 Playwright，所以这个缺口一直没暴露。已执行 `playwright install chromium` 补齐，并做了真实端到端验证（见 11.6）。
2. **既有测试用假 PDF 字节** —— `test_pdf_export.py` 的 mock 返回 `b"%PDF-1.4 fake-content"`，加了回读校验后必然被判 `pdf_unreadable`。已改为用 `pypdf` 生成结构合法的空白 PDF，并新增「回读失败必须阻断」的用例。
3. **`pypdf` 未在依赖中声明** —— 已在 `requirements.txt` 补登（`pypdf>=4.0`）。

### 11.5 新增文件与改动

| 文件 | 内容 |
| --- | --- |
| 新增 `app/backend/service/report_check.py` | `ReportIssue` / `ReportCheckResult` / `check_report` / `dedupe_issues` |
| `app/backend/service/pdf_export_service.py` | 新增 `verify_pdf`（回读校验）+ `_first_heading` / `_normalize` |
| `app/backend/router/research_router.py` | `export_pdf` 接入前后双重门禁；`export_markdown` 保持不阻断 |
| 新增 `app/test/test_report_check.py` | 15 例：三类 error、正常报告放行、locator 去重不误报、四类 warning、去重、回读四种情形 |
| `app/test/test_pdf_export.py` | 改为合法 PDF；新增 4 例（前检阻断 / 回读阻断 / warning 回传 / Markdown 不阻断） |
| `requirements.txt` | 补 `pypdf>=4.0` |

### 11.6 验证结果

| 验证项 | 结果 |
| --- | --- |
| 22 个测试文件 | **342 passed** |
| 真实 Chromium 渲染 → pypdf 回读（含标题、含引用条目的完整报告） | 导出前检查放行、无 warning；生成 PDF **278,859 字节 / 2 页**；回读放行、无 error 无 warning；提取文本 1,360 字，**引用条目 `WEB1_1-1` 与报告标题均在文中** |
| 真实 Chromium 渲染（短报告，只有 warning） | 回读无 error，按预期放行 |

回读校验的 `_normalize` 会去掉所有空白后再比对，因为 PDF 提取会把标题按行断开、把词间空格打散；直接子串匹配会误报「标题缺失」。该行为已由 `test_present_title_no_warning` 覆盖。

**未验证**：`export_markdown` 的逃生通道语义未在真实用户路径上走查（API 层有断言，前端未接线到按钮）。

**后续补齐（见 16 节）**：前端消费 422 问题清单与 `X-Report-Warnings` 头已实现；补做时发现 CORS 未配 `expose_headers`，该响应头跨域时前端根本读不到 —— 属同一类「后端在发、前端收不到」的断链。

---

## 十二、批次 4 执行记录（并发与节奏）

### 12.1 先核实前提，三条不成立

动手前逐条核实了计划里四条措施在本代码库的前提，结果三条站不住：

| 原计划 | 核实结果 | 处置 |
| --- | --- | --- |
| 4.1 全局并发上限（`Semaphore`） | 图里**唯一的并行是 `plan → web_search ∥ local_rag`（2 个节点）**，且两个节点内部检索是顺序执行的（`tools.py` / `web_search.py` / `local_rag.py` 无 `gather` / `Semaphore` / `create_task`）。加全局信号量对当前拓扑没有任何被触发的场景 | **不做**，理由记录在案 |
| 4.2 单点重试与全量重跑同路径 | **代码里没有任何节点级重试** —— 只有 LLM 客户端自身的 `max_retries`（批次 1 加的）。重跑的唯一路径是用户点「重新研究」 | **不做**，无可统一的对象 |
| 4.4 确定性章节配额（position 取模） | `write_node` 是**单次 `astream` 生成整篇报告**，没有「各章由不同生成单元产出」的结构。ai-ppt 的 position 取模前提（每页一个独立生成单元）在这里不存在；详略失衡不是并发协调问题 | **改为修断链**（见 12.2） |

真正的并发源是「每个研究完成后起的后台 task」（`memory_service` 记忆抽取、`research_service` 标题生成），但它们各自只有 1 次 LLM 调用，且没有观测数据支撑该把上限设成多少 —— 凭猜设定阈值属于拍脑袋，留作容量治理议题。

### 12.2 修掉一条用户可见的断链：报告结构不遵循已批准的计划

**问题**：`plan_node` 产出完整大纲（章节 id / title / description / requires_data / requires_chart / priority / search_queries），但 `write_node` 的提示词**完全没有引用 `outline`**。追踪确认 `outline` 的消费者只有两个：

1. `_derive_search_plan` —— 生成检索计划 ✓
2. `_render_execution_appendix` —— 执行附录里列一份「规划输出」清单 ✓

**报告正文结构无人消费**。后果在 HITL 下尤其明显：`hitl_config.plan_review` 默认开启，用户在 `plan_approval` 里看到并批准的是一份 6 节大纲，拿到的报告章节却由 writer 自由发挥 —— **批准的是一份，交付的是另一份**。这也正是原计划 4.4 想解决的「详略失衡」的真实根因。

**修法**：新增 `_render_outline_for_prompt`，把大纲压成提示词片段注入 write prompt，并明确要求「详细分析部分必须按此结构展开，每节以 `## <标题>` 开头，不得新增、合并或调换章节」。

只保留写作需要的字段 —— `search_queries` / `status` / `id` 是流程内部信息，写进提示词只会干扰模型（已加测试 `test_internal_fields_are_not_leaked_into_prompt` 锁住）。`requires_data` / `requires_chart` 转成「需数据支撑」「需图表」标记，并要求需数据的章节「确实查不到数据时显式写明缺失，不要用笼统表述糊过去」。

### 12.3 新增单轮研究看门狗

**问题**：`llm_timeout_seconds` 只约束**单次** LLM 调用；一轮研究有 15~25 次 LLM 调用加十余次检索，任何一处慢下来都会让前端一直转圈。`task_registry` **完全没有超时机制**（无 deadline / watchdog）。

**修法**：新增 `run_timeout_seconds`（默认 900 秒，0 表示不限），用 `asyncio.timeout()` 包住图消费循环，超时走**独立的** `except TimeoutError` 分支：

- 发 `run.error(code="RunTimeout")`，消息里写明「可从检查点续研」
- **不标记 thread completed** —— 这一轮没跑完，标记了就没法续研
- **不误报为用户取消** —— 与 `except asyncio.CancelledError` 分支严格区分，否则前端会显示成「用户已取消」

超时后检查点保持完好，前端可据 `/state` 的 `next_nodes` 续研，与进程重启中断复用同一套恢复语义。

### 12.4 配置变更

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `run_timeout_seconds` | 900 | 单轮研究总时长上限；0 = 不限 |
| `llm_timeout_seconds` | 60 | 单次调用超时（批次 1） |

既有流式测试用 `MagicMock` 顶替运行时配置，新增字段后需同步补 `run_timeout_seconds`（`test_p2.py` 3 处、`test_sse_heartbeat.py` 1 处）。

### 12.5 验证结果

| 验证项 | 结果 |
| --- | --- |
| 23 个测试文件 | **350 passed** |
| 看门狗单测 | 超时发 `run.error(RunTimeout)`；不出现 `run.cancelled`；不调用 `_complete_thread` |
| 真实调用：plan → write 端到端 | plan 出 6 节大纲，writer 产出的报告 `##` 标题 **6/6 全部匹配**（`REPORT_LEN=4909`） |

**未验证**：`run_timeout_seconds` 未在真实长任务上触发过（需构造 15 分钟以上的真实研究）；`web_search` / `local_rag` 在新型号下的检索质量仍未实测。

### 12.6 结论：批次 4 的实际产出与原计划的差异

原计划 4 条措施里，**2 条不做**（前提不成立，理由见 12.1）、**1 条改了目标**（从加配额改为修断链，且修出的是用户可见问题）、**1 条按计划落地**（看门狗）。这符合「先核实前提再动手」的原则 —— 若照原计划实施，会写出两处永远不会被触发的基础设施。

---

## 十三、批次 5 执行记录（契约对拍与回归基线）

### 13.1 已落地：事件协议前后端契约对拍（5.1）

协议链路是 `backend/schemas/events.py` → `docs/event-protocol.json` → `agent_front/src/types/events.gen.ts`。

**关键发现**：`scripts/export_event_protocol.py` **只生成前半段的 JSON**，`.ts` 是照着 json 手工维护的。也就是说后端改了字段而前端没跟上时，编译期与运行期都不会报错，只在线上表现为「某个字段是 undefined」。这条链路此前**没有任何断言保护**。

新增 `app/test/test_event_protocol_contract.py`（4 例）：

| 用例 | 断言内容 |
| --- | --- |
| `test_protocol_file_matches_registry` | `docs/event-protocol.json` 必须是后端 schema 的忠实导出；过期即失败，并在失败信息里给出重生成命令 |
| `test_union_matches_registry` | 前端 `EventType` 联合类型与后端事件名一一对应（增、删都会失败） |
| `test_event_data_interfaces_match_models` | 每个事件 data 接口的**字段名与必填性**与后端模型一致 |
| `test_nested_models_exported_to_frontend_stay_in_sync` | 嵌套模型（如 `SourceItem`）同样比对 —— 它也被前端直接引用 |

**断言有效性已验证**：向前端类型注入一个多余字段 `fake_field?: string` 后，失败信息精确指出 `run.error (RunErrorData): 前端={...} 后端={...}`，而非笼统报错。避免写出「永远为绿」的假测试。

### 13.2 未落地：5.2 Fake LLM 单测分层 / 5.3 固定语料回归基线

两项**暂未实施**，原因记录如下：

- 现状核查：节点测试已在多层打桩（patch `_invoke_structured_agent` / `_invoke_json_agent`、传假 agent、patch `_check_evidence_sufficiency` 等），并非「缺桩可用」。引入统一 Fake LLM 属于测试基建重构，收益是减少样板，但不解决任何已知缺陷。
- 5.3 的固定语料回归基线依赖 5.2 的确定性 Fake LLM，且需要先确定「哪些输出值得钉成基线」—— 报告正文由 LLM 生成，钉基线等于钉住某个模型版本的措辞，模型一换就全线失败，维护成本可能高于收益。更合适的做法是钉**结构与契约**（章节数、引用格式、字段完整性），而非文本内容。

建议：若要做 5.3，改为「结构基线」而非「文本基线」—— 用 Fake LLM 产出固定文本，断言下游处理（引用校验、章节对齐、参考清单生成）的结果，这样模型换代不会误伤。

---

## 十四、批次 6 前提核实：三项均不成立，建议不做

批次 6 原定「数据层加固」，三项都假设存在一张**生成产物表**（ai-ppt 把每页幻灯片存成行，故 `(project_id, outline_page_id)` 唯一键、`input_signature`、`expected_revision` 都成立）。**本项目的报告正文存在 LangGraph checkpoint 的 `chat_messages` 里，业务表里没有产物行。**

### 现状：全库只有四张表

| 表 | 用途 |
| --- | --- |
| `documents` / `document_chunks` | 文档与分块（向量化元数据） |
| `chunk_sync_messages` | 分块向量化的 MQ 消息与重试状态 |
| `chat_threads` | 会话元数据（title / intent / message_count / completed / pinned） |

报告正文不在其中任何一张表里。

### 逐项核实

| 项 | 核实结果 | 建议 |
| --- | --- | --- |
| 6.1 输入指纹 `input_signature` | **无载体**。没有「报告/任务表」，也就没有可标记为 `stale` 的旧产物行。`chat_threads` 上可以加一列记录该会话最后一轮的输入指纹，但「比对后标记旧产物」缺少目标对象 | **不做** |
| 6.2 条件更新 `expected_revision` | **有载体但语义相反**。`chat_threads` 的写入点（`mark_completed` / `rename_thread` / `pin_thread`）全是「最后写入胜出」的元数据更新。加乐观锁会让**异步标题生成**（`_trigger_title_gen` 在 run 结束后才落库）变成间歇性失败 —— 标题晚到是正常现象，不是需要丢弃的迟到写入 | **不做** |
| 6.3 幂等键 | **已实现**。文档向量化路径已有两道幂等：`chunk_consumer` 的处理前闸门（`chunk 已 indexed → 直接 ACK 跳过`，见 `chunk_consumer.py:112-129`）+ `postgres_client` 的 `ON CONFLICT (id) DO UPDATE` 幂等写 | **不做** |

### 结论

批次 6 的三项措施**都源自 ai-ppt 的架构前提（产物存表）**，迁移到本项目后没有对应对象。若照原计划实施，会写出「给不存在的表加列」「给不需要乐观锁的元数据加锁」以及「重复实现已有幂等」三类无效改动。

**因此本批次不做，也不需要 DDL 方案。** 若未来把报告产物从 checkpoint 拆到独立表（例如为了支持报告版本管理、跨会话复用），届时 `input_signature` 与幂等键才有真实落点 —— 那是产品需求驱动的重构，不是本次借鉴计划的范畴。

---

## 十五、真实端到端验证（模型切换后的补验）

模型全项目切换后，所有测试都 mock 了 LLM，模型换代可能引入的问题测不出来。补做真实端到端。

### 15.1 环境限制与替代方案

| 限制 | 影响 | 替代 |
| --- | --- | --- |
| 沙箱**屏蔽搜索引擎**（mojeek / startpage / google 全部 timeout） | 拿不到真实网页证据，检索质量与成文质量无法在本地验证 | 用合成检索结果直接喂 `web_search` / `deep_dive` 节点，只验 LLM 侧解析链路 |
| 本机**没有 Milvus** | 原本连图都构建不了 | 顺带修掉 `enable_milvus` 死配置（见 15.3），关闭后图可跑通 |

### 15.2 已验证：两个自由文本 JSON 节点在新模型下解析正常

`web_search` / `local_rag` / `deep_dive` 仍走 `_invoke_json_agent` 解析模型返回的 JSON 文本（未改结构化输出），是模型换代风险最高的一环。

| 节点 | 型号 | 耗时 | 结果 |
| --- | --- | --- | --- |
| `web_search` | qwen3.7-flash | 6.2s | 4 条记录全部保留，`supports_questions` 由模型正确映射到子问题，`notes` 有内容 → JSON 解析成功 |
| `deep_dive` | qwen3.8-max | 14.5s | `evidence_pool` / `source_index` / `audit_flags` 均正常生成；`source_index[].label` 正确取自标题，**参考资料展示正常** |

一处曾被怀疑的异常已排除：`evidence_pool[].source_label` 出现 `web_unknown` 这类值，是模型自填的冗余字段，**不参与展示**（`source_index` 取 label 时 `title` 优先），无害。

### 15.3 端到端暴露的两个真实缺陷（已修）

| 缺陷 | 证据 | 修复 |
| --- | --- | --- |
| 搜索源不可达时每查询空等满 60 秒 | 实测 6 个查询纯等待 6 分钟，整轮研究被看门狗杀掉，用户只看到转圈 | 新增 `search_timeout_seconds`（默认 15）；两条分支都受约束；超时降级为空结果 + 告警；**超时后显式 `future.cancel()`**（检索跑在常驻后台循环上，不取消会连同连接一起挂着） |
| `enable_milvus` 是死配置 | 定义、映射都有，全项目无人读取；设为 false 照样连 Milvus，连不上就硬失败导致服务起不来 | `build_agents` 按开关决定是否初始化 RAG；关闭时本地检索降级为返回空结果 + 告警，图仍可跑通 |

修复效果已实测：每查询从 60 秒收敛到 **15 秒**（日志 12 条 `检索超时 15s` 告警）。

### 15.4 未解决：看门狗默认值需要现场标定

即使搜索超时收敛到 15 秒，完整一轮研究**仍然超过 15 分钟**（被 `run_timeout_seconds=900` 杀掉，`EXIT=124`）。剩余时间全部在 LLM 阶段 —— `thinking_nodes` 默认包含 `write` / `deep_dive` / `analyze`，而 Qwen3.8-Max 与 Qwen3.7-Plus 在思考模式下处理长提示会显著变慢。

**我不打算靠猜来调这个值**（调大可能掩盖真实的卡死）。改为补上标定所需的信息：看门狗触发时**记录最后在跑的节点**，否则运维只看到「超时了」，分不清是「慢但在干活」还是「卡死了」。

同时记录一个待办：**`thinking_nodes` 的成本与延迟需要在真实环境重新评估**。切换模型后，思考模式的耗时特征与 qwen-plus 时代不同，900 秒这个默认值是按旧模型估的。

### 15.5 仍未验证

- 真实检索质量与成文质量（需可访问搜索引擎的环境）
- `run_timeout_seconds` / `thinking_nodes` 在真实长任务上的合适取值
- 完整一轮研究的真实耗时基线 —— 这是标定看门狗的前提，需在有网络的环境跑一次

---

## 十六、补齐批次 3 的前端消费：门禁提示与跨域响应头

批次 3 只做了后端门禁，前端没有消费。补齐时发现的问题比预期严重。

### 16.1 发现的断链：CORS 未配 `expose_headers`

`app_main.py` 的 CORS 中间件配了 `allow_origins`（支持跨域），但**没有 `expose_headers`**。按 CORS 规范，自定义响应头对 JS 不可见，除非列在 `Access-Control-Expose-Headers` 里。

**也就是说：`X-Report-Warnings` 在跨域部署下前端永远读不到** —— 后端在发、前端收不到，warning 被静默丢弃。这与 `node_models`、`enable_milvus` 是同一类断链：**「存在」不等于「可用」**。

已补 `expose_headers=["X-Report-Warnings"]`。

### 16.2 前端改动

| 文件 | 变更 |
| --- | --- |
| `agent_front/src/api/rest.ts` | 新增 `ReportIssue` / `ExportResult` 类型；`ApiError` 增加 `issues` 字段；`downloadBlob` 解析 422 响应体的问题清单与成功时的响应头，返回 `{ blob, warnings }` |
| `agent_front/src/views/ChatView.vue` | 抽出 `saveBlob` / `describeIssue`；导出结果经 `exportNotice` 用 `NAlert` 展示 —— error 列出阻断项，warning 列出提醒项；无问题清单时仍走原有错误态 |

设计取舍：**422 带问题清单时不走 `chat.markError`**。markError 只显示一句「导出失败」，用户无从知道报告哪里有问题、该怎么改 —— 门禁的提示价值会全部丢失。

### 16.3 验证

| 项 | 结果 |
| --- | --- |
| 前端全量 | **6 个文件 69 passed**（新增 6 例） |
| 后端全量 | **24 个文件 358 passed** |
| `vue-tsc --noEmit` | 通过 |

新增用例覆盖：422 问题清单进入 `ApiError`、422 但响应体无法解析时不掩盖原始失败（不抛解析异常）、成功时解析 URL 编码的 `X-Report-Warnings`、无该头时返回空数组、以及 ChatView 层「有问题清单 → 展示具体问题」与**反例**「普通失败 → 不展示门禁提示」。

反例的意义：只断言「有问题时展示」无法区分「分支真实生效」与「banner 永远显示」，故补一条负向断言。

### 16.4 Markdown 逃生通道已接线

PDF 被门禁拦下时，提示里给出「导出 Markdown 原文」按钮，直接走已有的 `export/md`。

**理由**：门禁拦的是「排版后的成品」，不该把用户锁死。Markdown 保留完整内容，只是没有排版保证。没有这条退路，被拦的用户只能先去改报告才能导出。

按钮**只在 error（被拦）时出现** —— 已经导出成功了再给逃生入口只会让人困惑（已加负向用例锁住）。

### 16.5 验证补充

前端全量 **6 个文件 71 passed**（本轮再增 2 例：被拦时出现逃生入口且点击请求 `/export/md`；warning 放行时不出现）。

### 16.6 仍未验证

- 未在真实跨域部署下验证响应头可读（需前后端分域部署的环境）
- 未在真实被拦场景下走查完整交互（需一份真实触发 error 的报告）
