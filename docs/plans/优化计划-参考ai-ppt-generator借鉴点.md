# 借鉴 ai-ppt-generator 优化：改了什么 / 什么没改 / 还剩什么

> 来源：`docs/dev/参考项目借鉴-ai-ppt-generator-分析.md` 的 A / B / C 三类借鉴点
> 状态：**批次 1–5 已执行完毕，批次 6 经核实不做**
> 重写日期：2026-09-17 —— 本文是结论性重写，替换此前「计划 + 逐批流水账」的写法。**文中每条结论均已回查当前代码核实**，不再是计划文本。

---

## 0. 一屏总览

| 批次 | 计划项数 | 实际 |
| --- | --- | --- |
| 1 工程加固 | 5 | 3 项落地（上传校验 / 超时重试 / 配置落位）+ 2 项只读审计 |
| 2 结构化输出 | 4 | 落地；方案中途修订一次（工具调用 → 原生 JSON Schema） |
| 3 导出质量门禁 | 5 | 落地，另补前端消费与 CORS 响应头 |
| 4 并发与节奏 | 4 | **1 项落地、1 项改向、2 项不做** |
| 5 契约与回归 | 4 | **1 项落地、3 项不做** |
| 6 数据层加固 | 3 | **全部不做**（架构前提不成立） |

**计划外但已落地**：模型换代到 Qwen3.7/3.8 系列、会话归属校验（堵住水平越权）、检索超时可配、`enable_milvus` 配置项生效、看门狗记录最后在跑的节点。

**一句话结论**：真正起效的是「让模型输出受约束」「让产物可被验证」「让契约漂移可被测试捕获」这三件事；原计划里从 ai-ppt 直接搬过来的并发协调与数据层措施，在本项目大多没有对应对象，照做会写出永不触发的基建。

---

## 1. 改了什么

### 1.1 决策节点输出受 schema 约束（批次 2）

| 落点 | 内容 |
| --- | --- |
| 新增 `app/mult_agents/output_schemas.py` | `IntentDecision` / `PlanDraft` / `AnalysisDraft` / `ReflectionDraft` 及嵌套模型，枚举与长度写成字段级硬约束 |
| `app/mult_agents/models.py` | 新增 `build_structured_agent`；`supports_json_schema` 白名单，配错型号**启动即抛错**并列出可选型号 |
| `app/mult_agents/nodes/_parsing.py` | 新增 `_invoke_structured_agent` 与 `StructuredOutputError`；**不做代码围栏剥离、不做正则截取** —— provider 已保证结构，宽容解析只会掩盖「约束失效」 |
| `nodes/intent.py`、`plan.py`、`analyze.py` | 四个决策节点改走结构化路径，失败显式告警后回退规则引擎 |

覆盖节点：`intent_router` / `plan` / `analyze` / `reflect`。

### 1.2 模型换代与按节点分档（计划外，第十节）

| 项 | 改前 | 改后 |
| --- | --- | --- |
| 默认模型 | `qwen-plus` | `qwen3.8-max` |
| `node_models` | **断链配置**：代码一直在读，配置层从未定义，取值恒为 `None` | 补齐 `BusinessSettings.node_models` 与 `AppConfig.node_models` 并打通 |
| 辅助链路（标题 / 摘要 / 记忆抽取） | 原生 SDK + `qwen-turbo` | 统一迁到 `build_aux_llm`，与主链路共用兼容通道与超时重试；**`ChatTongyi` 从生产代码中完全移除**（现仅存于测试与 `eval_metrics.py`） |
| 证据评分 | 复用主模型 | 新增 `scorer_model`（默认 `qwen3.7-flash`），该 LLM 在 `deep_dive` 高频调用，不跟随主模型承担 Max 档成本 |

当前分档以 `config.json` 为准（`write` 为控制成本已降到 `qwen3.7-plus`）：长文与深度分析用 Max，检索过滤与判定用 Flash。

### 1.3 报告导出质量门禁 + 回读校验（批次 3）

| 落点 | 内容 |
| --- | --- |
| 新增 `app/backend/service/report_check.py` | `ReportIssue(severity, section, code, message)` + `check_report`；error 阻断、warning 放行 |
| `pdf_export_service.py` | 新增 `verify_pdf`：用 `pypdf` 校验页数 > 0、可解析、报告标题确实存在 |
| `router/research_router.py:568-610` | `export_pdf` 前后双重门禁；`export_md` **不阻断**（内容逃生通道） |
| `app_main.py:336` | CORS 补 `expose_headers=["X-Report-Warnings"]` |
| 前端 `api/rest.ts`、`views/ChatView.vue` | 422 问题清单进 `ApiError.issues` 并用 `NAlert` 展示；被拦时给出「导出 Markdown 原文」入口（仅在 error 时出现） |

判定依据是「这份文件交出去会不会出错」，不是「写得够不够好」—— 后者不该由代码替用户决定。

### 1.4 报告结构不再脱离已批准的计划（批次 4 改向产物）

`plan_node` 产出的大纲原本只有两个消费者（检索计划、执行附录），**报告正文无人消费**。在 HITL 下表现为：用户在 `plan_approval` 里批准的是一份 6 节大纲，拿到的报告章节却由 writer 自由发挥。

新增 `write.py:_render_outline_for_prompt`，把大纲压成提示词片段注入 write prompt，并要求「每节以 `## <标题>` 开头，不得新增、合并或调换章节」。真实调用验证：plan 出 6 节，writer 产出的报告 `##` 标题 **6/6 匹配**。

### 1.5 单轮研究看门狗（批次 4）

`llm_timeout_seconds` 只约束**单次**调用，一轮研究有 15~25 次 LLM 调用加十余次检索。`task_registry` 此前没有任何超时机制。

新增 `run_timeout_seconds`（默认 900，0 = 不限），用 `asyncio.timeout()` 包住图消费循环，超时走**独立的** `except TimeoutError` 分支：发 `run.error(RunTimeout)`、**不标记 thread completed**（标记了就没法续研）、**不误报为用户取消**。后续补上超时日志带 `last_node`，便于区分「慢但在干活」与「卡死了」。

### 1.6 检索超时可配 + `enable_milvus` 生效（端到端暴露后修）

| 缺陷 | 修法 |
| --- | --- |
| 搜索源不可达时每查询空等满 60 秒，整轮被看门狗杀掉 | 新增 `search_timeout_seconds`（默认 15），两条分支都受约束；超时降级为空结果 + 告警，并显式 `future.cancel()`（检索跑在常驻后台循环上，不取消会连连接一起挂着） |
| `enable_milvus` 是死配置：定义与映射都有，全项目无人读取；设为 false 照样连 Milvus，连不上就硬失败 | `build_agents` 按开关决定是否初始化 RAG；关闭时本地检索降级为空结果 + 告警，图仍可跑通 |

实测：每查询从 60 秒收敛到 15 秒。

### 1.7 上传文件真实类型校验（批次 1）

新增 `app/backend/service/upload_guard.py`，接线到 `document_router`（`check_upload`）：扩展名白名单之后再按**魔数**校验 PDF / OOXML（含 `word/document.xml` 内部结构）/ OLE2 / 文本类，伪装文件返回 400。

### 1.8 LLM 超时与重试显式可配（批次 1）

`build_agent` 增加必填 `timeout` / `max_retries`，值来自 `BusinessSettings`（`llm_timeout_seconds` / `llm_max_retries`），并透传给辅助链路与评分器。

顺带发现：`ChatTongyi.max_retries` **默认是 10**，失败时会长时间重试，是请求长尾的真实来源，已显式收窄为 2。

### 1.9 会话归属校验：堵住水平越权（C3 审计发现，已修）

审计发现 `research_router` 中 `get_state` / `get_interrupt` / `get_history` / `export_markdown` / `export_pdf` 均声明了 `current_user` 却**未把 `user_id` 传入服务层**，服务层按 `thread_id` 直查——任何已登录用户拿到 `thread_id` 即可读取他人会话，导出接口还会**返回完整报告正文**。

已新增 `_assert_thread_access` / `require_thread_access` 依赖（`research_router.py:50-79`），所有 `thread_id` 作用域接口统一走该依赖；非本人会话返回 **404 而非 403**（403 等于承认该 ID 存在，可被用于枚举）。`allow_missing=True` 仅用于 `/run`、`/stream`、`/resume` 的「创建或续跑」语义。

> 注：本文前版曾把此项登记为「待决策、未修」，实际已于提交 `2b92c8b` 修复。

### 1.10 事件协议前后端契约对拍（批次 5.1）

协议链路是 `backend/schemas/events.py` → `docs/event-protocol.json` → `agent_front/src/types/events.gen.ts`。此前 `.ts` 是**照着 json 手工维护**的，后端改字段而前端没跟上时编译期与运行期都不报错，只在线上表现为「某个字段是 undefined」。

新增 `app/test/test_event_protocol_contract.py`（4 例）：协议文件必须与后端 registry 一致、前端 `EventType` 联合类型与后端事件名一一对应、每个事件 data 接口的**字段名与必填性**一致、嵌套模型同样比对。断言有效性已验证（注入多余字段后失败信息精确到字段名）。

### 1.11 依赖登记

`requirements.txt` 补登 `pypdf>=4.0`（此前在环境中可用但未声明）。

### 1.12 修掉 2 个遗留失败测试（重写本文时发现并修复）

`app/test/test_fix_regressions.py` 有两个用例长期是红的，均因生产代码演进后测试未同步：

| 用例 | 根因 | 修法 |
| --- | --- | --- |
| `TestAgentBuilderConsolidation::test_models_build_agents_uses_shared_collection_constants` | `_minimal_app_config` 默认 `enable_milvus=False`，`build_agents` 跳过 `init_rag_system`，桩函数没被调用 → `KeyError: 'cfg'` | 补 `enable_milvus=True`。该用例守的是「RAG 集合名一致」，而 RAG 初始化只在开启时发生 |
| `TestAgentBuilderResilience::test_settings_from_config_reach_build_agent` | 只 patch 了 `build_agent`，决策节点走结构化构造器未被拦截 → 真实创建客户端；传空 `api_key` 时 `ChatOpenAI` 回退读 `OPENAI_API_KEY`，**单独跑绿、整文件跑红**（前面的隔离 fixture 清过环境） | 同时 patch `build_structured_agent`，与同文件 `TestEnableMilvusActuallyGatesRagInit` 写法一致，不再依赖环境变量 |

第二个修复顺带扩大了覆盖：原先只断言普通节点，现在 **10 个节点的 timeout / max_retries 全部被断言**（含 4 个结构化节点）。已反向验证——若某个节点漏传配置，断言会失败。

---

## 2. 什么没改（明确不做，附理由）

### 2.1 原计划中前提核实不成立的 4 项

| 项 | 核实结果 | 结论 |
| --- | --- | --- |
| 全局并发上限 `Semaphore`（4.1） | 图里唯一的并行是 `plan → web_search ∥ local_rag`（2 个节点），且节点内部检索是顺序执行的，全项目无 `gather` / `create_task` | **不做** —— 没有会被触发的场景 |
| 单点重试与全量重跑同路径（4.2） | 代码里**没有任何节点级重试**，只有 LLM 客户端自身的 `max_retries`；重跑的唯一路径是用户点「重新研究」 | **不做** —— 无可统一的对象 |
| 确定性章节配额（4.4） | `write_node` 是单次 `astream` 生成整篇报告，没有「各章由不同生成单元产出」的结构；ai-ppt 的 position 取模前提（每页一个独立生成单元）不存在 | **改为修断链**（见 1.4），详略失衡的真实根因是 outline 无人消费 |
| 批次 6 全部三项 | 三项都假设存在**生成产物表**（ai-ppt 把每页幻灯片存成行）。本项目全库只有 4 张表（documents / document_chunks / chunk_sync_messages / chat_threads），**报告正文存在 LangGraph checkpoint 里，业务表里没有产物行** | **不做** —— 若照做会写出「给不存在的表加列」「给不需要乐观锁的元数据加锁」「重复实现已有幂等」三类无效改动 |

批次 6 逐项：`input_signature` 无载体（没有可标记 `stale` 的产物行）；`expected_revision` 会让**异步标题生成**变成间歇性失败（标题晚到是正常现象，不是需要丢弃的迟到写入）；幂等键**已实现**（`chunk_consumer` 处理前闸门 + `postgres_client` 的 `ON CONFLICT DO UPDATE`）。

### 2.2 主动不做的 3 项

| 项 | 理由 |
| --- | --- |
| `web_search` / `local_rag` / `deep_dive` 结构化 | 产出的是复杂嵌套的证据结构，且非决策节点。已在新型号下做过真实调用验证（`web_search` 6.2s、`deep_dive` 14.5s，解析均正常），当前自由文本 JSON 路径可用 |
| `write` / `direct_answer` 结构化 | 它们产出用户可见正文，结构化会破坏流式输出 |
| 测试分层 Fake LLM（5.2）与固定语料回归基线（5.3） | 节点测试已在多层打桩，并非「缺桩可用」，引入统一 Fake LLM 属测试基建重构，不解决任何已知缺陷；钉文本基线等于钉住某个模型版本的措辞，模型一换就全线失败。若要做，应改为**结构基线**（钉章节数、引用格式、字段完整性） |

### 2.3 审计发现但**未修**的 1 项

| 项 | 现状 |
| --- | --- |
| 认证 401 错误信息脱敏 | `auth/deps.py:43` 仍 `detail=str(exc)`，会把 PyJWT 的具体失败原因（签名失败 / 已过期）返回给调用方 |

C3 的其余三项经核实**已满足**：恒定时间比对（`auth/security.py` 用 `hmac.compare_digest`，用户不存在时以等长哑密码比对）、全局异常脱敏（`app_main.py` 只回通用文案 + trace_id）、文档删除按 `user_id` 过滤。C4（JSONB 就地改 dict）**不适用**：本项目用原生 psycopg + 显式 SQL，无 ORM 脏检查；两处 JSONB 更新均为显式语句，其中 `metadata = metadata || %s` 是服务端合并，比「读→改→整体赋值」更安全。

### 2.4 规则上主动放弃的 1 项

「引用编号悬空」原计划列为 error，实现时发现会**在每份正常报告上误报**：参考资料清单按 `locator` 对本地来源去重（同一文件的多个 chunk 只列一个代表），正文引用 `[LOC1_1-3]` 而清单只列 `[LOC1_1-1]` 是正常现象。改为检查更可靠的信号：**有引用就必须有非空的参考资料段落**，已加测试锁住。

---

## 3. 还存在什么问题

### 3.1 待决策（影响用户体验，需要权衡）

| 问题 | 说明 |
| --- | --- |
| **深度思考流式实际失效** | Qwen3.7/3.8 系列在兼容通道下**不透出 `reasoning_content`**（流式/非流式、`enable_thinking` 开与关四种组合实测，长度均为 0）。代码对空 reasoning 是安全的（仅在有内容时推送），但用户侧的「研究过程」会变安静。`thinking_nodes` 仍配置着 `write` / `deep_dive` / `analyze`，配置与实际表现已不一致 |
| **`run_timeout_seconds=900` 未标定** | 即使搜索超时收敛到 15 秒，完整一轮研究**仍超过 15 分钟**被看门狗杀掉，剩余时间全在 LLM 阶段。900 秒这个默认值是按 `qwen-plus` 时代估的，新型号在思考模式下处理长提示显著变慢。**没有靠猜调大**——调大会掩盖真实的卡死；需要一份真实耗时基线再定 |
| **`write` 降到 `qwen3.7-plus` 的质量未评估** | 为控成本从 `qwen3.8-max` 降档，但没有做质量对比，成本与质量的取舍目前没有数据支撑 |

### 3.3 待验证（环境受限，没有实机跑过）

| 问题 | 受限原因 |
| --- | --- |
| 真实检索质量与成文质量 | 沙箱**屏蔽搜索引擎**（mojeek / startpage / google 全部 timeout）；之前只验过用合成检索结果喂节点的 LLM 解析链路 |
| `run_timeout_seconds` / `thinking_nodes` 的合适取值 | 需在有网络的环境跑完整一轮，取真实耗时基线 |
| CORS `expose_headers` 在真实跨域下可读 | 需前后端分域部署 |
| Markdown 逃生通道的完整交互 | 需一份真实触发 error 的报告走查 |
| 上传校验的真实 HTTP 路径 | 目前只覆盖 `check_upload` 单元层，未走一遍真实上传 |

### 3.3 已知环境债

| 问题 | 说明 |
| --- | --- |
| `test_fix_regressions.py` 整文件运行会挂起 | 卡在需 RabbitMQ 的用例（`TestMqProducerDlqResilience` / `TestPublishMessagesHonoursReturnValue` 等），属既有环境依赖，与本次改动无关；验证时需按类运行或 deselect |
| `build_agent` 会写 `os.environ["DASHSCOPE_API_KEY"]` | 测试传入非空 `api_key` 会污染进程环境，导致 `.env` 相关用例失败。已在 `conftest.py` 加 autouse fixture 隔离，但根因（工厂函数写全局环境）仍在 |

---

## 4. 当前验证基线（2026-09-17 实测）

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 后端（排除已知挂起文件） | `pytest app/test --ignore=app/test/test_fix_regressions.py` | **536 passed, 2 skipped** |
| 回归专项 | `pytest app/test/test_fix_regressions.py`（deselect 3 个需 MQ 的类） | **116 passed, 7 deselected**（修复 1.12 后全绿） |
| 前端 | `vitest run` | **6 文件 71 passed** |

后端测试**当前无失败用例**（未跑的只有需 RabbitMQ 的 3 个类，属环境依赖）。

环境：conda `llmdev`（`D:\develop_tools\miniconda3\envs\llmdev\python.exe`）。

---

## 5. 建议下一步（按性价比排序）

1. **标定 `run_timeout_seconds`** —— 在有网络的环境跑一次完整研究，取真实耗时基线再定值；同时评估 `thinking_nodes` 在新型号下的成本与延迟。
2. **决定深度思考流式的去留** —— 要么换通道拿回 reasoning，要么把 `thinking_nodes` 与实际能力对齐，避免配置与表现不一致。
3. **补回需 RabbitMQ 的 3 个类** —— 起一套中间件让 `test_fix_regressions.py` 能整文件运行，否则「全绿」始终带一个星号。
4. （可选）**结构基线测试** —— 若要做回归基线，用 Fake LLM 产出固定文本，断言章节数、引用格式、字段完整性等结构，而不是钉住措辞。

**不建议做**：批次 6 的数据层加固、全局并发上限、确定性章节配额 —— 前提均不成立，理由见 2.1。
