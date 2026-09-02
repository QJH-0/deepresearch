# Phase 0 · 地基与止血 · 开发文档

> 依据：《DeepResearch_重构详细计划.md》v1.2 第六节 Phase 0
> 工期：0.5～1 天｜前置依赖：无｜后续依赖本 Phase 的：全部（事件协议是 P2/P6 的契约基础）
> 参考仓库：`vercel/ai-chatbot`（事件协议形态，commit `c2f8235`）

---

## 1. 目标与范围

**结论先行**：本 Phase 不新增业务功能，只做四件事——删死代码、统一配置、冻结事件协议、建重构分支。产出物 `schemas/events.py` + `docs/event-protocol.json` 是 Phase 2（后端流式）与 Phase 6（前端）的**唯一事件契约**，冻结后前端可立即 mock 开发。

**范围边界**：
- ✅ 做：删死代码、pydantic-settings、事件协议 pydantic 定义 + JSON 导出、git 分支
- ❌ 不做：不改任何运行时行为（现有非流式 /run 必须无回归）、不动 State/图/流式实现（P1/P2 的事）、不动前端

## 2. 现状锚点（已核实）

| 问题 | 位置 | 说明 |
|------|------|------|
| 旧节点实现与 nodes.py 并存 | `app/mult_agents/main.py`（551 行） | 一套无 HITL 的旧节点实现，同名函数静默覆盖隐患（确认清单问题 #8） |
| 根目录孤儿入口 | `D:\Code\LLMdev\deepresearch\main.py`（如存在） | 计划 P0-1 指定删除 |
| Bocha 残留测试 | `test_bocha.py`、`build_err.txt`（根目录） | 搜索已定 DuckDuckGo，Bocha 相关全删 |
| 配置混杂 | `.env` + `config.json`（根目录，含 api_key 等明文） | 两处配置，`app/backend/config/settings.py` 与 `app/mult_agents/config.py` 双头解析 |
| 事件定义缺失 | `app/backend/schemas/` 仅有 document/health/research.py | SSE 事件无统一 schema，零散 dict 直接下发 |

## 3. 任务分解

### P0-1 删除死代码

**涉及文件**：

| 动作 | 文件 |
|------|------|
| 删除 | `app/mult_agents/main.py`（551 行旧节点实现） |
| 删除 | 根目录 `main.py`、`test_bocha.py`、`build_err.txt`（存在即删） |
| 检查引用 | 删除后全局 grep `from mult_agents.main import`、`main_node`、`codegen_node`，确保无残留 import |

**实现要点**：
1. 删除前先确认 `graph.py:79-89`（`build_app`）import 的节点全部来自 `nodes.py`，与 `main.py` 无交集（已核实：`graph.py` 仅 `from .nodes import ...`）。
2. `codegen_node` 在 `nodes.py` 内如有定义且未接线，一并删除（计划 P1-2 也会处理，此处先删可减少 P1 拆包负担）。
3. **先测试后删**：删除后跑一次现有冒烟（启动后端 + 非流式 /run），行为无回归才算完成。

### P0-2 统一配置（pydantic-settings）

**目标**：`.env`（敏感项）+ `config.json`（业务项）合并为单一 `AppSettings`，出口在 `app/backend/config/settings.py`，`mult_agents/config.py` 改为从其读取（或保留薄转发层，P1 再彻底收敛）。

**实现要点**：

```python
# app/backend/config/settings.py（骨架）
from pydantic_settings import BaseSettings, SettingsConfigDict

class MiddlewareSettings(BaseSettings):
    """敏感/环境相关：.env 优先"""
    model_config = SettingsConfigDict(env_file=".env", env_prefix="", extra="ignore")
    postgres_dsn: str = "postgresql://root:postgres123@localhost:5432/mydb"
    redis_url: str = "redis://:redis123456@localhost:6379"
    rabbitmq_url: str = "amqp://admin:admin123456@localhost:5672/"
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    minio_endpoint: str = "localhost:9900"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    dashscope_api_key: str = ""          # 替代 config.json 中明文 api_key

class BusinessSettings(BaseSettings):
    """业务配置：config.json，允许热改"""
    model_config = SettingsConfigDict(json_file="config.json", extra="ignore")
    model: str = "qwen-plus"
    tenant_id: str = "default_tenant"
    user_id: str = "default_user"
    max_iterations: int = 3
    hitl_enabled: bool = True
    hitl_config: dict = {"plan_review": True, "analyze_clarify": True, "write_review": False}
    # …… 其余字段按现有 config.json 全量映射
```

**约束**：
- `config.json` 中的 `api_key` 明文迁入 `.env`（`DASHSCOPE_API_KEY`），`config.json` 删除该字段；`.env` 加入 `.gitignore`（如未加）。
- 兼容期：`mult_agents/config.py` 的 `AppConfig` 保持类名与字段访问方式不变，内部改为从新 Settings 取值，避免 P0 就大范围改调用点。
- **注意 pydantic-settings 的 JSON 支持版本**：`json_file` 需要 pydantic-settings ≥ 2.4；若版本不支持，用 `@model_validator` 手动加载 JSON 文件。

### P0-3 事件协议定义（本 Phase 核心交付物）

**目标**：在 `app/backend/schemas/events.py` 用 pydantic 定义全部 SSE 事件（单一事实源），并提供导出脚本生成 `docs/event-protocol.json` 供前端生成 TS 类型。

**事件清单**（照计划第三节，字段不得增删改名）：

| type | data 字段 | 说明 |
|------|-----------|------|
| `run.started` | thread_id, run_id | 一次研究开始 |
| `agent.status` | node, label, phase | 节点级进度 |
| `message.start` | message_id, role, node | 一条消息开始 |
| `message.delta` | message_id, text | token 级增量（核心） |
| `message.thinking` | message_id, text | 思考过程增量 |
| `sources.found` | sources[] | 新来源（url/title/snippet/source_type） |
| `interrupt.raised` | interrupt_id, kind, payload | kind: plan_approval / clarification / report_review |
| `run.completed` | message_id, final_state | 结束，报告 message_id |
| `run.cancelled` | reason | 用户取消完成 |
| `run.error` | code, message | 任何异常必发，随后关闭流 |

**代码骨架**：

```python
# app/backend/schemas/events.py
from typing import Literal, Union
from pydantic import BaseModel, Field
import time, uuid

class EventEnvelope(BaseModel):
    """SSE 行统一外层：data: {json}"""
    type: str
    ts: int = Field(default_factory=lambda: int(time.time() * 1000))
    data: dict

class SourceItem(BaseModel):
    url: str | None = None
    title: str = ""
    snippet: str = ""
    source_type: Literal["web", "kb"] = "web"
    chunk_id: str | None = None

class RunStartedData(BaseModel):
    thread_id: str
    run_id: str

class AgentStatusData(BaseModel):
    node: str
    label: str
    phase: str

class MessageStartData(BaseModel):
    message_id: str
    role: str = "assistant"
    node: str = ""

class MessageDeltaData(BaseModel):
    message_id: str
    text: str

class MessageThinkingData(BaseModel):
    message_id: str
    text: str

class SourcesFoundData(BaseModel):
    sources: list[SourceItem]

class InterruptRaisedData(BaseModel):
    interrupt_id: str
    kind: Literal["plan_approval", "clarification", "report_review"]
    payload: dict

class RunCompletedData(BaseModel):
    message_id: str
    final_state: str

class RunCancelledData(BaseModel):
    reason: str

class RunErrorData(BaseModel):
    code: str
    message: str

EVENT_REGISTRY: dict[str, type[BaseModel]] = {
    "run.started": RunStartedData,
    "agent.status": AgentStatusData,
    "message.start": MessageStartData,
    "message.delta": MessageDeltaData,
    "message.thinking": MessageThinkingData,
    "sources.found": SourcesFoundData,
    "interrupt.raised": InterruptRaisedData,
    "run.completed": RunCompletedData,
    "run.cancelled": RunCancelledData,
    "run.error": RunErrorData,
}

def event(type_: str, **data) -> EventEnvelope:
    model = EVENT_REGISTRY[type_](**data)
    return EventEnvelope(type=type_, data=model.model_dump())
```

**导出脚本**（`scripts/export_event_protocol.py`）：

```python
"""生成 docs/event-protocol.json：type → JSON Schema，供前端生成 TS 类型。"""
import json
from pathlib import Path
from app.backend.schemas.events import EVENT_REGISTRY

def main():
    out = {t: m.model_json_schema() for t, m in EVENT_REGISTRY.items()}
    path = Path("docs/event-protocol.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written: {path} ({len(out)} event types)")

if __name__ == "__main__":
    main()
```

**协议不变式**（写入 events.py 模块 docstring，P2 实现时必须满足）：
1. 流一定会结束：`completed / cancelled / error` 三者必有其一；finally 中只做"确保结束事件已发 + 关闭 generator"，不引用任何 try 块内变量（修复 `workflow_service.py:641` NameError 挂起 bug 的结构性保证）。
2. `message.delta` 按到达顺序追加即得完整文本。
3. 前端对未知 type 静默忽略（向前兼容）。

**参考实现**：`ai-chatbot`（vercel）的 AI SDK data stream protocol 事件形态——看 `参考项目/ai-chatbot` 的 stream protocol 类型定义文件（索引 2.2 节），学习其"事件类型枚举 + data 载荷 schema"的组织方式，**不照抄字段名**（那是 AI SDK 协议，本协议是自定义的）。

### P0-4 git 分支策略

```powershell
cd D:\Code\LLMdev\deepresearch
git checkout -b refactor/main
```

- 旧分支保留可回退；每个 Phase 合并前打 tag（`p0-done` … `p7-done`）。
- 注意：本仓库所在机器有"嵌套 .git 用 .gitignore 隔离"的既定约定——`参考项目/` 下 9 个参考仓库的嵌套 .git 必须已被 `.gitignore` 隔离，创建分支前 `git status` 确认无 untracked 嵌套仓库告警。

## 4. 测试计划

| 用例 | 类型 | 断言 |
|------|------|------|
| T0-1 事件 schema 完整性 | 单测 | `EVENT_REGISTRY` 覆盖第三节 10 种 type；每种 data 模型可从示例 dict 构造且非法字段报 ValidationError |
| T0-2 事件 envelope 格式 | 单测 | `event("run.started", thread_id="t", run_id="r")` 序列化为 `{"type","ts","data"}` 三键，ts 为毫秒 |
| T0-3 导出脚本幂等 | 单测 | 连续执行两次 `export_event_protocol.py`，JSON 内容一致 |
| T0-4 配置加载 | 单测 | 环境变量/JSON 各覆盖一个字段，`AppSettings` 取值正确；api_key 从 .env 而非 config.json 读取 |
| T0-5 启动无回归 | 冒烟 | `uvicorn app.app_main:app` 启动成功；`GET /health` 200；非流式 `POST /run` 行为与重构前一致（返回结构与耗时同一量级） |
| T0-6 死代码无残留 | 静态 | `grep -r "mult_agents.main\|codegen_node\|bocha" app/` 零命中 |

## 5. 验收清单

- [ ] `app/mult_agents/main.py`、根 `main.py`、`test_bocha.py`、`build_err.txt` 已删除，全局 grep 无残留引用
- [ ] `app/backend/config/settings.py` 统一配置生效，`config.json` 不再含明文 api_key
- [ ] `app/backend/schemas/events.py` 定义全部 10 种事件，T0-1～T0-3 单测通过
- [ ] `docs/event-protocol.json` 已生成并提交（前端契约冻结）
- [ ] `refactor/main` 分支已创建，后端可启动、非流式 /run 无回归（T0-5）
- [ ] 打 tag `p0-done`

## 6. 风险与对策

| 风险 | 对策 |
|------|------|
| 删 `mult_agents/main.py` 时有隐式引用（如脚本/测试直接 import） | 删前全局 grep；`app/test/`、`test/` 目录一并检查 |
| pydantic-settings JSON source 版本差异 | 锁定 `pydantic-settings>=2.4`；不支持则 model_validator 手动加载 |
| `.env` 迁移后启动脚本（`start_backend.bat`、`check_env.ps1`）读取路径失效 | 同步检查这两个脚本的环境变量引用 |
| config.json 中 api_key 已泄露进 git 历史 | 本次不处理历史重写（成本高）；确认 .gitignore 生效即可，密钥轮换由用户决定 |
