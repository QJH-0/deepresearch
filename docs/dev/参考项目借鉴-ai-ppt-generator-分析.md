# 参考项目分析：ai-ppt-generator 对本项目的可借鉴点

> 分析对象：`D:\Code\LLMdev\ai-ppt-generator`（FastAPI + LangChain/LangGraph + ARQ + React 的全栈 AI PPT 生成器，教学项目）
> 对照对象：本项目 DeepResearch（LangGraph 多智能体 + FastAPI + Vue3）
> 结论性质：技术选型与工程实践借鉴清单，非代码移植方案

---

## 一、项目概况

| 维度 | 内容 |
| --- | --- |
| 定位 | 输入主题/长文/文档 → 生成可改大纲 → 并发生成页面 → 在线编辑 → 导出原生可编辑 PPTX |
| 技术栈 | 后端 FastAPI + LangChain LCEL + LangGraph + ARQ + python-pptx + FontTools；前端 React + TS + TanStack Query；存储 PostgreSQL + Redis；另附一套 `java_backend/` 同构实现（Spring Boot） |
| 规模 | Python 后端约 25.7k 行 / 184 文件，其中测试 42 文件约 363 个用例；前端 100+ 源文件；`shared/` 契约 JSON 30+ 份 |
| 分层 | API 层 → 领域层（内容/版式/主题/质量）→ 工作流层（LLM 编排）→ 渲染层（PPTX） |

## 二、整体架构与数据流

自上而下五层（详见架构图）：

1. **前端 React SPA**：大纲工作台（三入口）、可视化编辑器（内容/版式/主题三面板）、进度与预览（SSE）。
2. **契约层 `shared/`**（横切，非调用层）：`themes/*.json`（4 套）、`layouts/*.json`（11 种固定版式）、`flex-presets/*.json`（18 个骨架预设）。前后端读**同一批物理文件**：后端 `app/core/paths.py` 指向 `SHARED_DIR`，前端 `import.meta.glob('../../../shared/...')` 构建期加载。
3. **FastAPI 接口层**：auth / projects / outlines / `deck/*`（pages、blocks、layout、ai_edit、export、generation）。
4. **ARQ 异步层**：页级扇出 + `asyncio.Semaphore(3)` 限流 + Redis pub/sub 事件总线 + 快照补发。
5. **领域与渲染层**：内容/版式/主题三分离数据模型 → 质量门禁（FontTools 字形度量）→ python-pptx 逐块构造 → 回读验证。

核心业务流：注册登录 → 创建项目 → 输入主题或材料 → 生成并确认大纲 → 并发生成页面 → 在线编辑 → 质量检查 → 导出 PPTX。

## 三、核心功能模块与实现思路

### 1. 大纲生成（`workflows/outline.py`）
LangGraph 结构极简：`prepare → generate` 两节点线性图，无图内自纠环。真正的纠错在生成器内的强校验（`llm/deepseek.py:133-152`）与 ARQ 任务级重试（`MAX_TRIES=2`）。输入裁剪：总 12000 字符 / 单节 2000 字符，整节追加、超长只截正文以保 `ref` 与文本不错位。

### 2. 结构化输出（`llm/client.py:56`）
`with_structured_output(schema, method="json_mode")` + LCEL `prompt | model`，无 repair 轮次，解析失败直接抛 `InvalidModelOutputError` 交任务重试兜底。Schema 不含 `id`/`locked` 等派生字段，由服务端补齐。

### 3. 单页生成（`workflows/slide.py` + `llm/slide.py`）
- **分层 prompt**：system（角色 + JSON 契约 + 硬约束）+ user（密度约束段 + JSON body）。
- **密度三档** `DensityProfile` 决定块数/要点数/字数带，并同源生成 prompt 约束段（`content_density.py`）。
- **页面节奏** `page_rhythm.py`：各页并发生成互相看不见，故用 `position` 取模**确定性**分配骨架与 callout 配额（`CALLOUT_EVERY=3`），保证整份 PPT 节奏而非逐页最优。
- **定向修复**：仅 error 与 `thin_content`/`empty_phrase` 触发 repair，`overflow`/`capacity` 不触发（`quality.py:407-419`），`MAX_REPAIR_ROUNDS=1`。

### 4. AI 编辑的工具调用（`llm/slide_edit.py` + `edit_tools.py`）
`bind_tools` 绑定 6 个 `replace_*` 及 flex 专属 `add_block/delete_block/change_type`。关键设计：
- 工具只改内存副本 `EditSession`，不写库；返回成功/错误字符串。
- 模型只能看到**可写快照**（不含 locked/image/chart 块），locked 块直接拒绝。
- 应用前 `filter_patches` 丢弃 locked/不存在/类型不符/重复操作**并给出原因**，`apply_patches` 幂等。
- 工具循环上限 4 轮，无 `tool_calls` 即停，再由 `_diff_operations` 反推操作列表给前端。

### 5. 页级并发生成（`worker/deck_tasks.py`）
`generate_deck` 只做「限流、逐页派发、汇报进度」，整份生成与单页重试走**同一条路径**。软取消用 Redis 标记 `deck:{id}:cancel`（TTL 30min），每页开始前检查，在跑的那页跑完即止——避免浪费已花出的 token。

### 6. 进度推送（`services/events.py` + `api/sse.py`）
Channel `{name}:{key}:events`，同时写快照 key `{name}:{key}:snapshot`（TTL 1h）。SSE 首帧先发快照，15s 无消息发 `: heartbeat`，命中 terminal 类型即结束。断线恢复靠「快照 + 端点按 DB 现算的 fallback 补发」，无 Last-Event-ID 事件回放。

### 7. 导出质量门禁（`domain/export_check.py` + `render/verify.py`）
- **error / warning 两级**：error 阻断导出（结构破坏、越界），warning 提醒但放行（溢出、内容单薄、重复页、无来源数字、低分辨率图、字体缺失）。
- **FontTools 字形级度量**：读 `getBestCmap/hmtx/unitsPerEm` 算真实字宽，缺字形退 `xAvgCharWidth`；字体缺失时降级为系数估算并打 `used_estimate` 标记。
- **导出后回读验证**：校验可打开、页数、16:9 画布、**禁止整页大图**（防截图承载正文）、形状不越界、原生表格/图表数量、关键文字完整性。

### 8. 双端同源与一致性
前端 `render/flexLayout.ts` 与后端 `domain/flex_solve.py` 是**逐行镜像**的同一套求解器；用 `shared/flex-presets/golden-*.json` 的 `expected_rects` 做逐字段断言（`scripts/flex-parity.mts`）。注意：这些 parity 脚本是手动运行，**无 CI 门禁**。

## 四、可借鉴清单（分点）

### A. 高优先级：直接补强本项目短板

**A1. 结构化输出替代手写 JSON 解析**
- 借鉴点：`with_structured_output(schema, method="json_mode")` + Pydantic 强 schema，把「模型输出必须可用」做成框架级保证。
- 本项目现状：`app/mult_agents/nodes/_parsing.py` 用 `_extract_json_block` 正则抠 `{}` + `json.loads` + 失败返回 `fallback`——静默降级，模型输出质量无法被约束。
- 适用场景：`intent` 意图分类、`plan` 子问题拆解、`analyze` 缺口判定、`reflect` 决策、引用条目抽取。
- 落地建议：为每个「结构化决策节点」定义 Pydantic 模型（含字段级 `max_length`/`min_length` 约束），改走 `with_structured_output`；保留 `_parsing.py` 仅作为非关键路径兜底。参考其做法：**schema 只放模型该产出的字段，id/locked 等派生字段由服务端补齐**。

**A2. 上传文件的真实类型校验**
- 借鉴点：`ingest/upload.py` 用 magic prefix 判类型 + 对 docx 进一步校验内部 `word/document.xml` 存在，防「改扩展名伪装」。
- 本项目现状：`app/backend/router/document_router.py:80-86` 仅按扩展名白名单过滤（分块读取的大小限制做得不错，已优于常见实现）。
- 适用场景：`POST /documents/upload`，以及未来任何用户上传入口。
- 落地建议：扩展名白名单之后加一层 magic bytes 校验；伪装文件返回 400 并记录审计日志。

**A3. 结果幂等键 + 输入指纹**
- 借鉴点：① `slides` 表以 `(project_id, outline_page_id)` 唯一键作幂等键，重跑自动续跑；② `project_outlines.input_signature` 存输入 SHA-256，防「旧结果绑到新输入」；③ worker 落库用 `with_for_update()` + `expected_revision` 比对，迟到的结果直接丢弃。
- 本项目现状：有 checkpointer 保证图状态可恢复，但业务表（报告/任务/记忆）缺少「输入变了旧产物必须作废」的显式机制。
- 适用场景：报告重新生成、文档重新向量化、`retry_document_vectorization` 重试链路。
- 落地建议：报告表加 `input_signature`（问题 + 检索参数 + 文档版本集合的 hash），生成前比对，不一致则标记旧报告 `stale` 而非静默覆盖；重试类接口补 `expected_revision` 条件更新。

**A4. LLM 调用显式超时与重试**
- 借鉴点：`ChatOpenAI(timeout=60, max_retries=2)`，且 ARQ 侧 `max_tries` 与业务重试常量同源，并明确「不抛 `arq.Retry` 会导致任务永远停在生成中」这一踩坑。
- 本项目现状：`app/mult_agents/models.py:43` 的 `ChatTongyi(model=..., temperature=..., streaming=True)` 未设 `timeout` / `max_retries`；`task_registry` 的状态机在 LLM 挂起时存在长尾风险。
- 适用场景：所有节点级 LLM 调用；尤其是 `write` 长文生成。
- 落地建议：模型工厂统一注入 `timeout` 与 `max_retries`；在 task_registry 中为运行中任务加「超时看门狗」，超时后置终态并发 `run.failed`，避免前端永久 loading。

### B. 中优先级：架构模式层面的借鉴

**B1. 分级门禁（error 阻断 / warning 放行）**
- 借鉴点：`export_allowed = 无 error`；warning 只提示不阻断，且按 `severity + 目标 + 字段 + message` 去重，避免同一问题刷屏。
- 本项目现状：PDF 导出（Playwright）目前无导出前检查、无导出后校验，失败即整体报错。
- 适用场景：报告导出、报告质量自检、引用完整性校验。
- 落地建议：定义 `ReportIssue(severity, section, code, message)`；error 例：无引用支撑的结论、引用编号悬空；warning 例：篇幅过短、单一信源占比过高、图表缺标题。前端用同一套分级渲染。

**B2. 产物「回读验证」**
- 借鉴点：`render/verify.py` 不只验证接口返回 200，而是把生成的文件重新打开，校验页数、画布比例、形状越界、关键文字确实存在，并明确禁止「整页大图」这种伪原生产物。
- 本项目现状：`pdf_export_service.py` 用 Playwright 出 PDF 后直接返回字节流，无任何产物级校验。
- 适用场景：PDF 导出、未来任何文件类产出。
- 落地建议：导出后用 pypdf 校验页数 > 0、可解析、关键章节标题字符串确实存在；不通过则降级返回 Markdown 附件并在响应中标注原因。

**B3. 页级扇出 + 信号量限流**
- 借鉴点：把「整份生成」拆成「调度器 + N 个独立子任务」，整份与单点重试共用一条路径；`asyncio.Semaphore` 控制对模型的并发压力。
- 本项目现状：`plan → web_search ∥ local_rag` 的并行靠 LangGraph 双出边实现，但**没有全局并发上限**；多子问题 `deep_dive` 若改为并行会瞬间打满上游配额。
- 适用场景：多子问题并行检索、并行深挖、批量文档向量化。
- 落地建议：引入模块级 `asyncio.Semaphore(config.max_llm_concurrency)`，所有 LLM/检索调用前 `async with`；「单点重试」与「全量重跑」共用同一个执行函数，避免两套代码分叉。

**B4. 确定性节奏控制（并发场景下的一致性）**
- 借鉴点：`page_rhythm.py` 因为各页并发生成、彼此不可见，改用 `position` 取模的**确定性**规则分配结构配额，让整份文档有节奏而非逐页最优。
- 本项目现状：报告各章节由不同节点/不同轮次生成，章节详略与结构风格缺少全局约束，易出现「某章过长、某章一笔带过」。
- 适用场景：`plan` 阶段的章节配额分配、`write` 阶段的篇幅与结构约束。
- 落地建议：在 plan 阶段就按 `position` 确定性下发「本章目标字数区间 / 是否需图表 / 是否需对比表」，写进各章 prompt，而不是让 writer 自由发挥。

**B5. 契约单一真源 + 生成 + 对拍断言**
- 借鉴点：① `shared/` JSON 物理共享，双端读同一批文件；② OpenAPI → TS 类型自动生成（`Makefile` 的 `gen-api`），并明确「不写 `response_model` 会导致生成类型退化为 object」；③ 镜像算法用 golden fixture 逐字段断言。
- 本项目现状：已有很好的雏形——`scripts/export_event_protocol.py` 从 `EVENT_REGISTRY` 生成 `docs/event-protocol.json`，前端已有 `agent_front/src/types/events.gen.ts`。**但缺三样**：无 parity/一致性断言测试、无 CI 门禁、生成动作靠手动执行。
- 适用场景：SSE 事件协议、引用数据结构、State 契约。
- 落地建议：为事件协议加一条「后端 Pydantic schema ↔ 前端 TS 类型字段集合相等」的自动断言测试；把 `export_event_protocol.py` 接进测试或 pre-commit，避免手改漂移。

### C. 工程与测试实践

**C1. 测试不消耗 API 额度**
- 借鉴点：`FakeQueue` + `FakeSlideGenerator` monkeypatch，直接以 ctx 调 `generate_deck`，让整条异步链路可在单测中跑完且零成本；`conftest.py` 在**导入前**设临时环境变量以绕开 `lru_cache` 单例，并用 autouse fixture 每例后 `engine.dispose()` 防 asyncpg 绑旧 event loop。
- 本项目现状：`app/test/` 有 40+ 测试文件与 mock fixture，方向一致；`eval_metrics.py` 走真实 LLM（LLM-as-Judge），成本高、不宜频繁跑。
- 适用场景：节点级单测、CI 快速回归。
- 落地建议：把「真实 LLM 评测」与「Fake LLM 单测」明确分成两套；后者进 CI 门禁，前者按需手动跑。

**C2. 固定语料 + 量化门禁的回归集**
- 借鉴点：`backend/regression/corpus/` 固定语料强制覆盖全部版式，runner 跑「密度 × 主题」组合，门禁是硬指标：可编辑性 100%、溢出槽位 < 5%、主题切换后内容指纹不变；字体缺失时自动放宽阈值并标注。
- 本项目现状：有 `eval_metrics.py` 的 7 项指标，但缺少「固定输入集 + 阈值门禁 + 可对比历史」的回归基线。
- 适用场景：RAG 检索质量回归、引用准确率回归、报告结构回归。
- 落地建议：建立固定问题集（覆盖 direct / 需澄清 / 多子问题 / 本地知识库为主 四类），每次改动跑一遍并把指标落盘为可比对的历史 JSON，设阈值告警。

**C3. 安全的细节习惯**
- 借鉴点：登录用预计算 dummy hash 做恒定时间比对防时序侧信道；越权访问返回 404 而非 403（不泄露资源存在性）；存储键由服务端生成 + 路径穿越校验；错误信息不外泄上游供应商响应与用户材料。
- 适用场景：`auth_router`、`document_router` 的文档读取/删除、MinIO 对象键生成。
- 落地建议：逐条对照自查，特别是「文档 ID 属于他人」时统一返回 404，以及异常信息脱敏后再返回前端。

**C4. OOXML / 结构化文档的踩坑记录方式**
- 借鉴点：`spAutoFit` 必须关闭、`normAutofit` 比例必须自算；OOXML 子元素（`a:ea` 排在 `a:latin` 之后、`spPr`/边框须按 schema 顺序插入）；JSONB 就地改 dict 不触发脏检查、必须赋新列表。每条都以注释形式钉在代码现场。
- 适用场景：本项目 PDF/HTML 导出、PostgreSQL JSONB 字段（如事件负载、引用列表）更新。
- 落地建议：JSONB 更新处统一改为「读 → 构造新对象 → 整体赋值」，避免 ORM 脏检查失效导致的静默丢更新。

### D. 谨慎借鉴 / 明确不建议照搬

| 项 | 原因 |
| --- | --- |
| 图生图→图库→占位图三级降级链 | 本项目无图片生成需求；但「每级降级都要能被观测到」的思路可迁移到检索源降级（DDG 失败 → 本地 RAG 兜底）。 |
| 受约束 contenteditable 与光标稳定算法 | 前端框架不同（React vs Vue3），本项目也无块级富文本编辑器；仅在将来做报告在线编辑时参考。 |
| flex 布局树双端求解器 | 强绑定 PPT 场景，本项目无对应需求。其「镜像算法 + golden fixture 对拍」的方法论已在 B5 中吸收。 |
| ARQ 队列 | 本项目已有 RabbitMQ + 自研 `task_registry`，栈不同；借鉴其**任务语义**（页级扇出、同路径重试、软取消）而非框架。 |
| `java_backend/` 同构实现 | 教学项目为 Java 求职者提供的第二实现，双份代码维护成本高，本项目不需要。 |
| SSE 无事件回放 | 这一点**本项目更强**：已有 heartbeat + `/resume` 断点续研。ai-ppt 仅靠快照补偿，反而可作为对照说明本项目设计更完整。 |

## 五、一句话总结

ai-ppt-generator 最值得拿走的不是任何一段代码，而是三类**工程约束习惯**：
1. **让模型输出受 schema 约束**，而不是解析后静默降级；
2. **让产物可被验证**（导出前检查 + 导出后回读），而不是相信接口返回 200；
3. **让契约只有一份真源并对拍断言**，而不是靠双端开发者互相记得同步。
