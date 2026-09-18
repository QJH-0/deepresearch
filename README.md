# DeepResearch

> 多智能体深度研报助手 —— LangGraph 编排 + FastAPI + Vue3，从检索取证到成文导出全链路可恢复、可观测。

**环境要求**：conda 环境 `llmdev`（Python 3.11 + 全量依赖）。运行任何测试前必须先切到该环境。

## 能力概览

| 能力 | 实现要点 |
| --- | --- |
| 多智能体研究 | LangGraph 状态图，10 个节点：意图路由（可直答）→ 澄清 → 规划 → 双路检索 → 证据裁判 → 分析 → 补搜 → 成文 |
| 结构化决策 | 7 个节点输出受 JSON Schema 约束（`ProviderStrategy` + `strict`），provider 侧强制，非法输出直接暴露而非静默降级 |
| 流式交互 | SSE 10 种事件；打字机输出 + 研究过程折叠卡片；心跳保活与断线重连续流 |
| 人在环路 | LangGraph interrupt 三点：澄清 / 大纲审批 / 报告审阅（可按需开关） |
| 取消与续研 | 检查点持久化，进程重启或主动停止后可从断点续研 |
| 报告导出 | PDF（Playwright 渲染）+ 导出前质量门禁 + 导出后回读校验；被拦时提供 Markdown 逃生通道 |
| 引用溯源 | 论断绑定来源 ID，参考资料清单自动去重拼接 |
| 知识库 | 文档上传（魔数校验）→ RabbitMQ 异步向量化 → 混合检索（向量 + PG 关键词 + RRF 融合 + 重排） |
| 会话记忆 | langmem + PostgresStore，热路径召回 + 后台抽取 |
| 可观测性 | 单次调用超时/重试、单轮研究看门狗（超时记录最后在跑节点）、检索超时快速失败 |

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                 前端 Vue3 + Pinia + Naive UI                 │
│  ChatView ←→ Pinia Stores ←→ useEventStream (SSE reducer)   │
└──────────────────────────┬──────────────────────────────────┘
                           │ JWT + SSE /stream /resume
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                       后端 FastAPI                          │
│  auth │ research │ document │ admin │ health                 │
│  ResearchService (async gen) │ TaskRegistry │ MemoryService  │
│  导出：report_check → PDF 渲染 → verify_pdf 回读校验          │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    LangGraph 编排层                          │
│  intent → clarify → plan →(web_search ∥ local_rag)          │
│  → deep_dive → analyze →(reflect ↺ | write) → END           │
│  State 分组 + reducer │ HITL interrupt │ Postgres 检查点     │
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
│   │   ├── auth/                  # JWT 鉴权与用户解析
│   │   ├── config/                # pydantic-settings（.env + config.json）
│   │   ├── infra/                 # postgres / redis / milvus / minio / mq 客户端
│   │   ├── router/                # auth / research / document / admin / health
│   │   ├── schemas/               # 事件协议（EVENT_REGISTRY）与请求模型
│   │   └── service/               # research / memory / task_registry / document
│   │                              # pdf_export_service / report_check / upload_guard
│   ├── mult_agents/               # LangGraph 多智能体层
│   │   ├── graph.py               # 图拓扑与条件路由
│   │   ├── state.py               # State 分组 + reducer
│   │   ├── models.py              # 模型工厂：按节点分档 + 结构化执行体
│   │   ├── output_schemas.py      # 各节点的输出 schema
│   │   ├── runtime.py             # AgentBundle + checkpointer 工厂
│   │   ├── tools.py               # 检索链（ddgs → tavily → searxng）+ RAG
│   │   ├── prompts.py             # 提示词模板
│   │   ├── nodes/                 # 节点实现（intent / plan / web_search / ...）
│   │   └── rag/                   # RAG 检索、RRF 融合、重排
│   └── test/                      # 后端测试套件
├── agent_front/                   # 前端 Vue3 + TypeScript
│   ├── src/{api,components,composables,router,stores,types,utils,views}
│   └── test/                      # vitest 用例
├── data/knowledge/                # 知识库样例文档
├── docs/
│   ├── dev/                       # 分阶段开发文档（Phase 0-8）
│   ├── refactor/                  # 重构批次记录与测试用例
│   ├── plans/                     # 优化计划与执行记录
│   ├── interview/                 # 面试专题
│   └── event-protocol.json        # 事件协议导出（前后端契约对拍基准）
├── scripts/                       # 运维与工具脚本（事件协议导出、DLQ 重放等）
├── config.json                    # 业务配置（模型分档、HITL 开关、超时阈值）
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

cd D:\Code\LLMdev\deepresearch
pip install -r requirements.txt

# PDF 导出依赖浏览器内核，首次需安装
playwright install chromium

cd agent_front && npm install
```

### 2. 配置 `.env`

```ini
# 阿里云百炼 API Key（必需）
DASHSCOPE_API_KEY=sk-your-api-key
MODEL=qwen3.8-max

# 鉴权（必需）
JWT_SECRET=your-secret
AUTH_USERS=admin:your-password

# PostgreSQL（业务表 + checkpoint + 记忆）
POSTGRES_DSN=postgresql://root:postgres123@localhost:5432/mydb
REDIS_URL=redis://:redis123456@localhost:6379
MILVUS_HOST=localhost
MILVUS_PORT=19530
RABBITMQ_URL=amqp://admin:admin123456@localhost:5672/
ENABLE_MEMORY=true
CHECKPOINTER_BACKEND=postgres
ENABLE_MILVUS=true
```

> 模型、HITL 开关、超时阈值、节点级模型分档等业务配置在 `config.json`，修改后无需改代码。

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

## 测试

统一使用 conda `llmdev` 环境（managed Python 3.13 无 pytest）。

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

### 当前基线

| 范围 | 结果 |
| --- | --- |
| 后端（排除 `test_fix_regressions.py`） | **536 passed, 2 skipped** |
| 后端回归专项（deselect 3 个需 MQ 的类） | **116 passed, 7 deselected** |
| 前端 | **6 文件 71 passed** |

跳过的 2 个是 `test_p1_smoke.py` 的端到端冒烟用例，需要真实 `DASHSCOPE_API_KEY` + 可连接的 PostgreSQL。

> `test_fix_regressions.py` 整文件运行会挂起在需 RabbitMQ 的用例上，这是环境依赖而非缺陷 —— 无中间件时请按上面的方式 deselect。

## 关键技术决策

| 决策 | 说明 |
| --- | --- |
| 模型选型 | 默认 `qwen3.8-max`，按节点分档（`node_models`）：长文与深度分析用 Max，检索过滤与判定用 Flash，兼顾质量与成本 |
| 结构化输出 | `create_agent(response_format=ProviderStrategy(schema, strict=True))`，由 provider 强制 schema。**必须显式传 `ProviderStrategy`** —— 自动策略选择依据型号名白名单，qwen 不在内，传裸 schema 会退化成工具调用策略 |
| 输出约束纪律 | 结构化节点的提示词**不得**规定输出格式（格式由 schema 承担）；结构化失败显式抛错，是否降级由各节点自行判断 |
| 深度思考 | 兼容通道不透出 `reasoning_content`，`thinking_nodes` 默认为空 —— 开启只会付出延迟与成本而用户无感知 |
| 检索链 | ddgs → tavily → searxng 链式降级，单查询超时可配并快速失败 |
| Checkpointer | PostgreSQL（`AsyncPostgresSaver`），降级链 Postgres → Redis → 内存 |
| 记忆系统 | langmem + PostgresStore 双通道 |
| 流式输出 | FastAPI SSE + async generator，心跳保活 |
| 事件协议 | 10 种事件类型，Pydantic schema + `EVENT_REGISTRY`，前后端契约由测试对拍 |
| 文档向量化 | RabbitMQ 异步解耦 + 消费前幂等闸门 + DLQ |
| 前端 | Vue3 + Pinia + Naive UI，`useEventStream` 统一事件 reducer |

## 事件协议

定义见 `app/backend/schemas/events.py`，导出文件 `docs/event-protocol.json` 是前后端契约对拍基准（`app/test/test_event_protocol_contract.py` 会校验它未过期）。

| 类型 | 说明 |
| --- | --- |
| `run.started` | 研究任务启动 |
| `agent.status` | 节点状态变更 |
| `message.start` | 消息开始 |
| `message.delta` | 流式 token |
| `message.thinking` | 思考过程 |
| `sources.found` | 检索到信息源 |
| `interrupt.raised` | HITL 中断 |
| `run.completed` | 任务完成 |
| `run.cancelled` | 任务取消 |
| `run.error` | 任务异常 |

## 文档索引

- [分阶段开发文档](docs/dev/) — Phase 0-8 全流程
- [重构批次记录](docs/refactor/) — 检索链路 / 流式交互 / 可靠性运维 / 缺陷收口
- [优化计划与执行记录](docs/plans/) — 含借鉴点分析与 LLM 链路收敛
- [事件协议](docs/event-protocol.json) · [测试用例清单](docs/dev/test-cases.md)
- [部署与中间件](docs_konwledge/) — 数据库初始化、中间件使用指南、工程问题记录
