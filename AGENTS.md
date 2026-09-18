# AGENTS.md — DeepResearch 项目约定

> **全局治理规则不在本文件**。安全与版本控制底线（含切分支三道闸）、多 Agent 协作与委派边界、验证与排障规范、生产级质量底线、门禁机制，见 `C:\Users\20448\.ai-shared\AGENTS.md`，**动手前必读**。
> 本文件只写本项目专属的、无法从全局规则推导的约定。

## 项目定位

多智能体深度研报系统：LangGraph 编排 + FastAPI 后端 + Vue3 前端。从意图路由、双路检索、证据裁判到成文导出全链路，具备取消续研、HITL 审批、引用溯源与导出质量门禁。

LangGraph 官方文档：https://reference.langchain.com/python/langgraph/overview —— 方法定义以该文档**最新版本**为准，本仓依赖版本见 `requirements.txt`。

## 环境（硬约束）

| 项 | 值 |
| --- | --- |
| Python | conda `llmdev` — `D:\develop_tools\miniconda3\envs\llmdev\python.exe`（3.11）。**managed 3.13 无 pytest，跑测试必须用 llmdev** |
| 前端 | `agent_front/`，`npx vitest run` |
| 浏览器 E2E | `agent-browser` 不支持 Windows，无法执行；需要时改用 Playwright |
| 嵌套仓库 | `参考项目/` 下有多个独立 git 仓库，**勿在项目根执行影响全树的 git 操作** |

## 测试基线（2026-09-18 实测）

```bash
# 后端全量（排除需 RabbitMQ 的回归文件）
python -m pytest app/test --ignore=app/test/test_fix_regressions.py -q

# 回归专项（deselect 3 个需要 RabbitMQ 的类）
python -m pytest app/test/test_fix_regressions.py -q \
  --deselect app/test/test_fix_regressions.py::TestMqProducerDlqResilience \
  --deselect app/test/test_fix_regressions.py::TestPublishMessagesHonoursReturnValue \
  --deselect app/test/test_fix_regressions.py::TestRetryFailedChunksKeepsMessageIds

cd agent_front && npx vitest run
```

| 范围 | 结果 |
| --- | --- |
| 后端（排除回归文件） | 536 passed, 2 skipped |
| 后端回归专项 | 116 passed, 7 deselected |
| 前端 | 6 文件 71 passed |

`test_fix_regressions.py` **整文件运行会挂起**在需 RabbitMQ 的用例上（环境依赖，非缺陷），无中间件时必须按上面的方式 deselect。

## 架构约定

### 模型与结构化输出

- 模型分档在 `config.json` 的 `node_models`，**不硬编码**；默认 `qwen3.8-max`，长文用 Max、判定用 Flash
- 结构化节点（7 个：`intent_router` / `plan` / `reflect` / `web_search` / `local_rag` / `deep_dive` / `analyze`）走 `create_agent(response_format=ProviderStrategy(schema, strict=True))`
- **必须显式传 `ProviderStrategy`** —— 自动策略选择依据型号名白名单（只认 grok / gpt-5 / gpt-4.1 / o3 等），qwen 不在内，传裸 schema 会退化成工具调用策略
- 结构化节点的提示词**不得规定输出格式**（格式由 schema 承担）；`direct_answer` / `write` / `clarify` 产出用户可见正文，不结构化
- 结构化失败一律抛 `StructuredOutputError`，**是否降级由各节点自行判断**并留痕，不静默
- `thinking_nodes` 默认为空：兼容通道不透出 `reasoning_content`，开启只付延迟与成本
- 结构化节点配了不支持 `json_schema` 的型号会**启动即失败**（`JSON_SCHEMA_MODELS` 白名单），这是有意的

### 图与状态

- 拓扑：`intent →(direct_answer | clarify → plan)→(web_search ∥ local_rag)→ deep_dive → analyze →(reflect ↺ | write)`
- 节点只直调函数，**不经过 agent tool-calling**（所有 agent 的 `tools` 必须为空，有测试锁住）
- 检查点持久化在 PostgreSQL，取消/重启后从断点续研

### 关键可观测性配置（`config.json`）

`llm_timeout_seconds`（单次调用）/ `llm_max_retries` / `run_timeout_seconds`（单轮研究看门狗，超时记录 `last_node`）/ `search_timeout_seconds`（单查询检索超时）

## 目录导航

| 路径 | 内容 |
| --- | --- |
| `app/backend/` | 路由、服务、基础设施客户端、事件协议（`schemas/events.py`） |
| `app/mult_agents/` | 图拓扑、State、模型工厂（`models.py`）、输出 schema（`output_schemas.py`）、节点（`nodes/`） |
| `app/test/` | 后端测试；`test_fix_regressions.py` 为断链回归聚集地 |
| `agent_front/` | Vue3 + Pinia + Naive UI，`useEventStream` 统一事件 reducer |
| `docs/` | `dev/` 分阶段文档、`refactor/` 重构记录、`plans/` 优化计划、`interview/` 面试专题 |
| `docs/event-protocol.json` | 前后端事件契约对拍基准（`test_event_protocol_contract.py` 校验其未过期） |

## 项目内踩坑记录

- `_build_llm` 曾写 `os.environ["DASHSCOPE_API_KEY"]` 污染进程环境（已移除）；测试传空 `api_key` 时 `ChatOpenAI` 会回退读环境变量，导致「单独跑绿、整文件跑红」
- `TestThinkingNodesConfig` 一类用例构造 `AppConfig` 时**型号必须是支持 json_schema 的**，否则结构化节点构造即抛错
- 契约/回归断言补完后**必须注入一次人为漂移**确认它真会失败（见全局规则的验证规范）
- 报告导出前跑 `report_check`、导出后跑 `verify_pdf` 回读；`export/md` 不阻断（内容逃生通道）
