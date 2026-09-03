# DeepResearch

> 多智能体深度研报助手 — 基于 LangGraph + FastAPI + Vue3 的 AI 研究系统

## 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                      前端 Vue3 + Pinia + Naive UI            │
│  ChatView ←→ Pinia Stores ←→ useEventStream (SSE reducer)   │
└──────────────────────────┬──────────────────────────────────┘
                           │ SSE /stream /resume
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    后端 FastAPI                              │
│  research_router │ ResearchService (async gen) │ TaskRegistry│
│  MemoryService (langmem) │ DocumentService                    │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                 LangGraph 层（拓扑）                         │
│  intent → clarify → plan → web_search/local_rag → deep_dive  │
│  → analyze → reflect → write                                 │
│  State 分组 + reducer │ HITL interrupt (3 点)               │
└──────────────────────────┬──────────────────────────────────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         PostgreSQL    Milvus     DuckDuckGo
         (checkpoint    (向量)     (web search)
          + memory)    MinIO      Redis
                       RabbitMQ
```

## 目录结构

```
deepresearch/
├── app/                      # 后端应用
│   ├── app_main.py           # FastAPI 入口
│   ├── backend/              # 后端服务层
│   │   ├── config/           # pydantic-settings 配置
│   │   ├── infra/            # 基础设施客户端 (store_client, mq, redis, milvus, minio)
│   │   ├── router/           # FastAPI 路由 (research, health, documents)
│   │   ├── schemas/          # 事件协议 (events.py, requests.py)
│   │   └── service/          # 业务服务 (research_service, memory_service, task_registry, document_service)
│   ├── mult_agents/          # LangGraph 多智能体层
│   │   ├── graph.py          # 图拓扑定义
│   │   ├── state.py          # State 分组 + reducer
│   │   ├── models.py          # 模型工厂 (build_agents)
│   │   ├── runtime.py         # AgentBundle + 运行时
│   │   ├── tools.py           # SearchProvider (DuckDuckGo) + RAG 工具
│   │   ├── prompts.py         # 提示词模板
│   │   ├── nodes/             # 节点实现 (intent, plan, clarify, web_search, ...)
│   │   └── rag/               # RAG 检索逻辑
│   └── test/                 # 测试套件
│       ├── conftest.py        # 共享 mock + fixture
│       ├── test_events.py     # 事件协议
│       ├── test_state.py      # State 契约
│       ├── test_p1.py         # 拓扑/reducer/搜索
│       ├── test_p2.py         # 流式输出
│       ├── test_p3.py         # 取消/恢复
│       ├── test_p4.py         # HITL
│       ├── test_p5.py         # 记忆
│       ├── test_p7.py         # 引用溯源
│       ├── test_citations.py  # 引用去重
│       ├── test_stream_events.py
│       ├── test_search_provider.py
│       ├── test_resume.py
│       ├── test_hitl.py
│       └── test_memory.py
├── agent_front/              # 前端 Vue3
│   ├── src/
│   │   ├── api/              # API 调用
│   │   ├── components/       # 组件 (chat, icons, knowledge)
│   │   ├── composables/      # useEventStream
│   │   ├── stores/           # Pinia stores
│   │   ├── views/            # ChatView
│   │   └── utils/            # 工具函数
│   ├── package.json
│   ├── vite.config.ts
│   └── Dockerfile            # 前端容器 (多阶段: node build → nginx)
├── data/knowledge/           # 知识库文档
├── docs/                     # 开发文档
│   ├── dev/                  # 分阶段开发文档 (Phase 0-8)
│   ├── event-protocol.json   # 事件协议定义
│   └── archive/              # 归档的历史规划文档
├── docs_konwledge/           # 技术知识库 + 中间件编排
│   ├── docker-compose.middleware.yml  # 中间件一键拉起
│   └── 工程问题与解决方案记录.md
├── scripts/                  # 脚本
│   ├── export_event_protocol.py
│   └── split_nodes.py
├── config.json               # 业务配置 (无敏感信息)
├── requirements.txt           # 后端依赖
├── pyproject.toml             # pytest 配置
├── Dockerfile                # 后端容器 (多阶段构建)
├── docker-compose.app.yml    # 应用编排 (backend + frontend + rabbitmq)
└── .dockerignore
```

## 快速开始

### 1. 环境准备

#### 方式 A: conda 环境安装（推荐）

项目使用 conda 环境 `llmdev`，Python 3.11 + 全量依赖：

```bash
# 创建 conda 环境
conda create -n llmdev python=3.11 -y
conda activate llmdev

# 安装后端依赖
cd D:\Code\LLMdev\deepresearch
pip install -r requirements.txt

# 安装前端依赖
cd agent_front
npm install
```

#### 方式 B: 最小环境

如果只运行后端测试，`conftest.py` 会自动 mock 未安装的第三方包，无需安装 `langgraph`、`langchain` 等重依赖：

```bash
pip install fastapi uvicorn pydantic pydantic-settings pytest pytest-asyncio python-dotenv duckduckgo-search
```

#### 配置 .env 文件

在项目根目录创建 `.env`：

```ini
# 阿里云百炼 API Key（必需）
DASHSCOPE_API_KEY=sk-your-api-key

# 模型配置
MODEL=qwen-plus
USER_ID=default_user
TENANT_ID=default_tenant
MAX_ITERATIONS=3

# PostgreSQL (记忆 + Checkpoint)
POSTGRES_DSN=postgresql://root:postgres123@localhost:5432/mydb

# Redis
REDIS_URL=redis://:redis123456@localhost:6379

# Milvus (向量检索)
MILVUS_HOST=localhost
MILVUS_PORT=19530
MILVUS_COLLECTION=mult_agent_memory

# 记忆系统
ENABLE_MEMORY=true
CHECKPOINTER_BACKEND=postgres
ENABLE_MILVUS=true
```

### 2. 启动中间件

```bash
docker compose -f docs_konwledge/docker-compose.middleware.yml up -d
```

中间件包含：PostgreSQL、Redis、Milvus (etcd + MinIO)、MySQL、Neo4j

### 3. 启动应用

```bash
# 方式 A: Docker Compose（推荐）
docker compose -f docker-compose.app.yml up -d --build

# 方式 B: 本地开发
# 后端
pip install -r requirements.txt
cd app && uvicorn app_main:app --reload --port 8000

# 前端
cd agent_front && npm install && npm run dev
```

### 4. 访问

- 前端: http://localhost:8080 (Docker) 或 http://localhost:5173 (本地开发)
- 后端 API: http://localhost:8000
- API 文档: http://localhost:8000/docs
- 健康检查: http://localhost:8000/health/live

## 测试

### 后端单元测试

```bash
# 确保 conda 环境激活
conda activate llmdev

# 全量测试（llmdev 环境：269 passed, 2 skipped）
cd D:\Code\LLMdev\deepresearch
python -m pytest app/test/ -v --tb=short

# 也可以用 conda run 直接执行
conda run -n llmdev python -m pytest app/test/ -v --tb=short

# 单独运行某个测试文件
python -m pytest app/test/test_p1.py -v
python -m pytest app/test/test_p5.py -v

# 含覆盖率报告
python -m pytest app/test/ --cov=backend --cov=mult_agents --cov-report=term-missing
```

#### 测试结果汇总

| 环境 | 结果 | 说明 |
|------|------|------|
| **conda llmdev** (Python 3.11 + 全量依赖) | **269 passed, 2 skipped** | langgraph/psycopg 等真实安装 |
| **base conda** (Python 3.14 + 仅基础包) | **267 passed, 4 skipped** | conftest.py 自动 mock 未安装的包 |

**跳过的测试**：
- `test_p1_smoke.py` (2 tests): 端到端冒烟测试，需要 `langgraph` 真实安装 + `DASHSCOPE_API_KEY` + PostgreSQL 可连接

#### Mock 策略

`conftest.py` 使用 `_WildcardMockFinder` 自动 mock 未安装的第三方包：

| 环境 | langgraph | psycopg | langchain | mock 策略 |
|------|-----------|---------|-----------|-----------|
| llmdev | ✅ 真实 | ✅ 真实 | ✅ 真实 | 不 mock，使用真实包 |
| base | ❌ 未安装 | ❌ 未安装 | ❌ 未安装 | 自动 mock，注册具体 mock 类 |

> `_WildcardMockFinder` 会先用 `PathFinder.find_spec` 检查包是否真实安装，仅在未安装时返回 mock。这确保在 `llmdev` 环境下不会误 mock 真实包的子模块（如 `psycopg_c.pq`）。

### 前端测试

```bash
cd agent_front && npx vitest
```

## 关键技术决策

| 决策 | 说明 |
|------|------|
| 搜索引擎 | DuckDuckGo (duckduckgo-search 纯 pip 依赖，无服务容器) |
| Checkpointer | PostgreSQL (langgraph-checkpoint-postgres) |
| 记忆系统 | langmem + PostgresStore 双通道 (删除旧哈希伪向量) |
| 流式输出 | FastAPI SSE + async generator (删除 Thread+Queue 桥接) |
| HITL | LangGraph interrupt (3 点: clarify / plan_approval / report_review) |
| 事件协议 | 10 种事件类型，pydantic schema 校验，EVENT_REGISTRY 注册 |
| 前端 | Vue3 + Pinia + Naive UI，useEventStream 统一事件 reducer |

## 事件协议

事件类型定义见 `docs/event-protocol.json`，schema 见 `app/backend/schemas/events.py`。

| 类型 | 说明 |
|------|------|
| `run.started` | 研究任务启动 |
| `agent.status` | 智能体节点状态变更 |
| `message.start` | 消息开始 |
| `message.delta` | 流式 token |
| `message.thinking` | 思考过程 |
| `sources.found` | 检索到信息源 |
| `interrupt.raised` | HITL 中断 |
| `run.completed` | 任务完成 |
| `run.cancelled` | 任务取消 |
| `run.error` | 任务异常 |

## 开发文档

- [分阶段开发文档](docs/dev/README.md) — Phase 0-8 全流程
- [事件协议](docs/event-protocol.json) — SSE 事件 schema
- [工程问题记录](docs_konwledge/工程问题与解决方案记录.md)
- [中间件指南](docs_konwledge/DeepResearch中间件使用指南.md)
