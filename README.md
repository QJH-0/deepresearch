# DeepResearch

> 多智能体深度研报助手 —— LangGraph 编排 + FastAPI + Vue3。从意图判定、双路检索、证据裁判到成文导出全链路可观测、可中断、可续研。

**环境要求**：conda 环境 `llmdev`（Python 3.11 + 全量依赖）。跑任何测试前必须先切到该环境，managed Python 3.13 无 pytest。

## 能力概览

| 能力 | 实现要点 |
| --- | --- |
| 多智能体研究 | LangGraph 状态图，10 节点 / 10 个 Agent。意图路由（可直答）→ 澄清 → 规划 → 双路并行检索 → 检索充分性裁判 → 证据裁判 → 分析 → 成文 |
| 两级自适应循环 | **内层**：`retrieve_grader` 判定本轮检索够不够，不够就改写检索词再搜；**外层**：`analyze` 的 `next_action` 决定要不要再走一轮研究。层级不同，互不混用 |
| 结构化决策 | 7 个节点输出受 JSON Schema 约束；无工具走 `ProviderStrategy(strict)`，带工具走 `ToolStrategy`，非法输出显式暴露，是否降级由各节点自行判断并留痕 |
| 工具调用 | `fetch_url` 是唯一绑给 agent 的工具（`deep_dive`），带 SSRF 防护；检索类入口仍由节点直调 |
| 流式交互 | SSE 10 种事件；token 级打字机 + 研究过程卡片；心跳保活；前后端契约由测试对拍 |
| 人在环路 | LangGraph interrupt 三点：计划审批 / 证据缺口处置 / 报告审阅，`hitl_config` 可逐项开关 |
| 取消与续研 | PostgreSQL 检查点；`resume` 三种模式（continue / answer / modify）覆盖崩溃续跑、HITL 回答、改条件重跑 |
| 报告导出 | Playwright 渲染 PDF；导出前质量门禁 `report_check`、导出后 `verify_pdf` 回读；被拦时提供 Markdown 逃生通道 |
| 引用溯源 | 角标 `[WEB1_1-1]` / `[LOC1_1-3]`；正则的唯一实现在 `mult_agents/citations.py`，写节点与质量门禁共用 |
| 知识库 | 上传（魔数校验）→ RabbitMQ 异步向量化 → 混合检索（查询重写 + 向量多路 + PG 关键词 + RRF 逐路融合 + 重排 + Parent-Child 扩展） |
| 会话记忆 | langmem + PostgresStore，热路径召回 + 后台抽取 |
| 可观测性 | 单次 LLM 超时/重试、单轮研究看门狗（超时记录最后在跑的节点）、单次检索超时快速失败 |
| 自动化评测 | 规则型指标层（纯函数可单测，用于回归哨兵）+ LLM-as-Judge 脚本（离线跑批） |

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                 前端 Vue3 + Pinia + Naive UI                 │
│  ChatView ←→ Pinia Stores ←→ useEventStream (SSE reducer)   │
│  PlanApproval / EvidenceGap / ReportReview / ProcessCard     │
└──────────────────────────┬──────────────────────────────────┘
                           │ JWT + SSE  /stream  /resume
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                       后端 FastAPI                          │
│  auth │ research │ document │ admin │ health                 │
│  ResearchService (async gen) │ TaskRegistry │ MemoryService  │
│  导出：report_check → PDF 渲染 → verify_pdf 回读校验         │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    LangGraph 编排层                          │
│  intent →(direct_answer | clarify → plan)                   │
│  plan →(web_search ∥ local_rag) → retrieve_grader           │
│        ↑──────── 内层重检（≤ max_retrieval_rounds）────────┘ │
│  retrieve_grader → deep_dive → analyze                      │
│        ↑──────── 外层续研（≤ max_iterations）──────────────┘ │
│  analyze → write → END                                      │
└──────────────────────────┬──────────────────────────────────┘
                           │
        ┌──────────┬───────┴────────┬──────────────┐
        ▼          ▼                ▼              ▼
   PostgreSQL    Milvus          Redis         RabbitMQ
   checkpoint    (向量检索)      (任务状态)     (文档向量化)
   + 业务表      MinIO(对象存储)
```

## 目录结构

```
deepresearch/
├── app/
│   ├── app_main.py                # FastAPI 入口（lifespan 初始化中间件与图）
│   ├── backend/
│   │   ├── auth/                  # JWT 鉴权与用户解析（身份只来自令牌）
│   │   ├── config/                # pydantic-settings（.env + config.json + 热更新）
│   │   ├── infra/                 # postgres / redis / milvus / minio / mq 客户端
│   │   ├── router/                # auth / research / document / admin / health
│   │   ├── schemas/               # 事件协议（EVENT_REGISTRY）与请求模型
│   │   └── service/               # research / memory / task_registry / document
│   │                              # pdf_export_service / report_check / upload_guard
│   ├── mult_agents/               # LangGraph 多智能体层
│   │   ├── graph.py               # 图拓扑与条件路由（含两级循环）
│   │   ├── state.py               # State 分组 + reducer 语义
│   │   ├── models.py              # 模型工厂：节点分档 + 结构化执行体
│   │   ├── output_schemas.py      # 各节点的输出 schema
│   │   ├── runtime.py             # AgentBundle + checkpointer 工厂 + recursion_limit
│   │   ├── tools.py               # 检索 Provider 链 + fetch_url（唯一 agent 工具）
│   │   ├── citations.py           # 引用角标的唯一实现
│   │   ├── eval_metrics.py        # 规则型质量指标（可离线单测）
│   │   ├── prompts.py             # 提示词模板
│   │   ├── nodes/                 # 节点实现 + _parsing / _context / _evidence / _fallbacks
│   │   └── rag/                   # RAG 检索、RRF 融合、重排、切分
│   └── test/                      # 后端测试（含 golden_set.json 与评测脚本）
├── agent_front/                   # 前端 Vue3 + TypeScript
│   ├── src/{api,components,composables,router,stores,types,utils,views}
│   └── test/                      # vitest 用例
├── data/test_documents/           # 知识库样例文档与测试产物
├── docs/
│   ├── dev/                       # 分阶段开发文档
│   ├── refactor/                  # 重构批次记录
│   ├── plans/                     # 优化计划与执行记录
│   ├── interview/                 # 面试专题
│   └── event-protocol.json        # 事件协议导出（前后端契约对拍基准）
├── scripts/                       # 运维与工具脚本（事件协议导出、DLQ 重放等）
├── config.json                    # 业务配置（模型分档、循环上限、HITL 开关、超时阈值）
├── requirements.txt               # 后端依赖
├── pyproject.toml                 # pytest 配置
├── Dockerfile / docker-compose.*.yml
└── start_backend.bat / check_env.ps1 / diagnose.ps1
```

## 快速开始

### 1. 安装依赖

```bash
conda create -n llmdev python=3.11 -y
conda activate llmdev

pip install -r requirements.txt

# PDF 导出依赖浏览器内核，首次需安装
playwright install chromium

cd agent_front && npm install
```

> `primp` 必须 `>=2.0.1`。2.0.0 有 TLS bug，会让 DuckDuckGo 全后端报 `cannot decrypt peer's message` / `tls handshake eof`，表现为「搜索全部失败、降级为空结果」。
> 不要把 `mcp>=2.0` 装进本项目环境：会把 `starlette` 拉到 1.6.0，与 `fastapi 0.123` 的 `<0.51` 约束冲突，后端直接不可用。

### 2. 配置 `.env`

```ini
# 阿里云百炼 API Key（必需）
DASHSCOPE_API_KEY=sk-your-api-key
MODEL=qwen3.8-max

# 鉴权（必需）
JWT_SECRET=your-secret
AUTH_USERS=admin:your-password

# 中间件
POSTGRES_DSN=postgresql://root:postgres123@localhost:5432/mydb
REDIS_URL=redis://:redis123456@localhost:6379
MILVUS_HOST=localhost
MILVUS_PORT=19530
RABBITMQ_URL=amqp://admin:admin123456@localhost:5672/
ENABLE_MEMORY=true
CHECKPOINTER_BACKEND=postgres
ENABLE_MILVUS=true
```

> 模型分档、循环上限、HITL 开关、超时阈值、证据预算等在 `config.json`，改完无需改代码（支持热更新）。
> `.env` 的 `HTTP_PROXY` 会被 shell 注入的同名变量盖掉（`load_dotenv` 默认不覆盖）。跑评测或脚本时用 `env -u HTTP_PROXY -u HTTPS_PROXY ...` 让 `.env` 生效。

### 3. 启动中间件

```bash
docker compose -f docker-compose.middleware.yml up -d
```

| 服务 | 宿主机端口 | 凭据 | 用途 |
| --- | --- | --- | --- |
| PostgreSQL (pgvector) | 5432 | `root` / `postgres123` | 业务表 + checkpoint + 记忆 |
| Redis | 6379 | `redis123456` | 任务状态、取消广播 |
| Milvus (gRPC) | 19530 | — | 向量检索 |
| MinIO | 9900 / 9901 | `minioadmin` / `minioadmin` | 对象存储（API / 控制台） |
| RabbitMQ | 5672 / 15672 | `admin` / `admin123456` | 文档向量化（AMQP / 管理台） |

### 4. 启动应用

```bash
# 方式 A：容器编排
docker compose -f docker-compose.app.yml up -d --build

# 方式 B：本地开发
cd app && uvicorn app_main:app --reload --port 8000   # 或双击 start_backend.bat
cd agent_front && npm run dev
```

| 端点 | 地址 |
| --- | --- |
| 前端（本地开发 / Docker） | http://localhost:5173 / http://localhost:8080 |
| 后端 API 与文档 | http://localhost:8000 · http://localhost:8000/docs |
| 健康检查 | http://localhost:8000/health/live |

主要接口：`POST /research/stream`（SSE 流式）· `POST /research/run` · `POST /research/resume`（`mode=continue|answer|modify`）· `POST /research/cancel` · `GET /research/state/{thread_id}` · `GET /research/threads/{thread_id}/interrupt` · `POST /research/threads/{thread_id}/export/pdf` · `POST /document/upload` · `POST /auth/login` · `GET /admin/config`

## 测试

统一使用 conda `llmdev` 环境。

```bash
# 后端全量（排除需 RabbitMQ 的回归文件）
python -m pytest app/test --ignore=app/test/test_fix_regressions.py -q

# 回归专项（deselect 3 个需要 RabbitMQ 的类）
python -m pytest app/test/test_fix_regressions.py -q \
  --deselect app/test/test_fix_regressions.py::TestMqProducerDlqResilience \
  --deselect app/test/test_fix_regressions.py::TestPublishMessagesHonoursReturnValue \
  --deselect app/test/test_fix_regressions.py::TestRetryFailedChunksKeepsMessageIds

# 前端
cd agent_front && npx vitest run
```

### 当前基线（2026-09-19 实测）

| 范围 | 结果 |
| --- | --- |
| 后端（排除 `test_fix_regressions.py`） | **668 passed, 2 skipped**（约 54 秒） |
| 后端回归专项（deselect 3 个需 MQ 的类） | **116 passed, 7 deselected** |
| 前端 | **6 文件 71 passed** |

跳过的 2 个是 `test_p1_smoke.py` 的端到端冒烟用例，需要真实 `DASHSCOPE_API_KEY` + 可连接的 PostgreSQL。

> `test_fix_regressions.py` 整文件运行会挂起在需 RabbitMQ 的用例上，这是环境依赖而非缺陷 —— 无中间件时请按上面的方式 deselect。

## 关键技术决策

| 决策 | 说明 |
| --- | --- |
| 模型分档 | `config.json` 的 `node_models` 按节点配：长文与深度分析用 `qwen3.8-max`，判定与检索整理用 `qwen3.7-flash`，成文用 `qwen3.7-plus`。不在代码里硬编码型号 |
| 结构化输出 | `create_agent(response_format=...)`。**无工具的节点用 `ProviderStrategy(schema, strict=True)`**（provider 侧 JSON Schema 强约束）；**带工具的节点必须用 `ToolStrategy`** —— 原生 `json_schema` 与 `tool_calls` 不兼容，模型先返回工具调用（content 为空）会让框架拿空 content 解析 JSON 而抛错 |
| ProviderStrategy 必须显式传 | 自动策略选择依据型号名白名单（只认 grok / gpt-5 / gpt-4.1 / o3 等），qwen 不在内，传裸 schema 会退化成工具调用策略 |
| 型号白名单 | `JSON_SCHEMA_MODELS` 限定结构化节点可用型号，配错**启动即失败**（有意为之），避免运行期才发现 schema 校验错误 |
| 输出约束纪律 | 结构化节点的提示词**不规定输出格式**（格式由 schema 承担）；结构化失败抛 `StructuredOutputError`，是否降级由各节点自行判断并留痕 |
| 直调 vs 工具 | 检索类入口由节点直调（任务路径可预测，workflow 比 agent 更可控更便宜）；只有「哪些证据可疑、要不要读原文」是运行时才知的，才绑成 agent 工具 |
| 深度思考 | `enable_thinking` 始终显式下发（部分型号默认开启，不关会让判定节点付出成倍延迟）；`thinking_nodes` 默认为空 —— 兼容通道不透出 `reasoning_content`，开启只付延迟与成本 |
| 检索源 | 当前 `search_providers` 收敛为单一 `ddgs`。Provider 链支持 `ddgs` / `ddgs_mcp` / `tavily` / `searxng`，但 tavily 需 Key、searxng 需自建，未配置时启动即告警说明哪些源被跳过 |
| 检索限流 | 一次研究要发 17~44 次查询，DuckDuckGo 会限流。用模块级锁 + 1.5 秒最小间隔（`DDG_MIN_INTERVAL_SECONDS`）保证并发检索时只有一个请求在飞 |
| recursion_limit | B1 引入内层重检后超步数远超默认的 25（`max_iterations=2` 就会撞顶，整轮研究失败）。改由 `recursion_limit_for()` 按循环上限推算并留 10 步余量 |
| Checkpointer | 异步链 `AsyncPostgresSaver` → `AsyncRedisSaver` → `InMemorySaver`。生产走 `astream`，同步 `PostgresSaver` 无 `aget/aput`，会落到基类 stub 抛 `NotImplementedError` |
| 证据预算 | 证据跨轮累积，节点每轮塞全量会让 token 线性增长且长上下文召回下降。`_context` 统一按条数 + 字符预算取 top-k；被裁掉的证据仍由 `deep_dive` 确定性回填补进证据池（拿先验分而非 LLM 裁判分） |
| 引用角标 | 正则的唯一实现在 `mult_agents/citations.py`（原先散落三处，改一处漏两处）。正文非法角标直接删除而非保留 —— 那意味着溯源断链 |
| 流式输出 | FastAPI SSE + async generator，无 Thread/Queue 桥接；心跳包装器超时不取消内部 future |
| 事件协议 | 10 种事件类型，Pydantic schema + `EVENT_REGISTRY`，前后端契约由 `test_event_protocol_contract.py` 对拍 |
| 文档向量化 | RabbitMQ 异步解耦 + 消费前幂等闸门 + DLQ |
| 前端 | Vue3 + Pinia + Naive UI，`useEventStream` 统一事件 reducer |

## 事件协议

定义见 `app/backend/schemas/events.py`，导出文件 `docs/event-protocol.json` 是前后端契约对拍基准。

| 类型 | 说明 |
| --- | --- |
| `run.started` | 研究任务启动（含 thread_id / run_id） |
| `agent.status` | 节点状态变更（node / label / phase / detail） |
| `message.start` | 消息开始 |
| `message.delta` | 流式 token |
| `message.thinking` | 思考过程增量 |
| `sources.found` | 检索到信息源 |
| `interrupt.raised` | HITL 中断（kind: plan_approval / clarification / evidence_gap / report_review） |
| `run.completed` | 任务完成（含 final） |
| `run.cancelled` | 任务取消 |
| `run.error` | 任务异常（含看门狗超时 `RunTimeout`） |

## 评测

两套互补，不互相替代：

- **规则型指标**（`app/mult_agents/eval_metrics.py`）：纯函数、零外部依赖、可离线单测。用于**回归哨兵** —— 改一处代码后立刻能看出证据有没有变脏、检索有没有变浅、引用有没有失效。指标含 `evidence_duplication_rate` / `retrieval_rounds` / `citation_legality` / `citation_coverage` / `key_point_coverage` / `expected_source_recall`。
  - `citation_coverage` 是**护栏指标不是判别指标**：未带角标的句子约 80% 是分析推演与结构性标记，~50% 已接近结构性天花板；它抓得住「链路故障」，但衡量不了检索深度。
  - `key_point_coverage` 只有要点本身有判别力时才有意义。要点写成「框架名 + 通用概念」时，跑 1 轮的 baseline 也能拿满分。
- **LLM-as-Judge**（`app/test/eval_metrics.py`）：需真实模型与中间件，离线跑批，不纳入 CI。题集数据化在 `app/test/golden_set.json`。

## 文档索引

- [分阶段开发文档](docs/dev/) · [重构批次记录](docs/refactor/) · [优化计划与执行记录](docs/plans/)
- [面试专题](docs/interview/) — 最满意项目与难点攻克 / 评测体系与指标来源 / 高并发承载与优化路线
- [事件协议](docs/event-protocol.json) · [测试用例清单](docs/dev/test-cases.md)
- [部署与中间件](docs_konwledge/) — 数据库初始化、中间件使用指南、工程问题记录
