# Phase 8 · 收尾（可选，不阻塞）· 开发文档

> 依据：《DeepResearch_重构详细计划.md》v1.2 第六节 Phase 8、第七节迁移策略
> 工期：另计（穿插进行）｜前置依赖：Phase 0-7 主线完成｜性质：不阻塞交付，按需推进
> 明确不做：langfuse 可观测性（决策 9，本次不纳入；本地 `参考项目/langfuse` 已备，后续接入直接用）

---

## 1. 目标与范围

**结论先行**：三个收尾项——测试补全（事件协议/resume/记忆检索单测体系化）、应用容器化（Dockerfile 补全，**无新增搜索服务编排**，web_search 是纯库调用）、工程文档更新。全部不阻塞主线交付。

## 2. 任务分解

### P8-1 测试补全

现状：仅 `test_multi_turn` 等零散测试（确认清单 J2）。目标测试矩阵：

| 模块 | 测试文件 | 覆盖（从各 Phase 测试计划汇总） |
|------|----------|-------------------------------|
| 事件协议 | `app/test/test_events.py` | T0-1～T0-3（schema 完整性/envelope/导出幂等） |
| 事件协议合规 | `app/test/test_stream_events.py` | T2-5（全事件过 EVENT_REGISTRY 校验）+ T2-3/T2-4（必发结束事件） |
| State 契约 | `app/test/test_state.py`、`test_nodes_contract.py` | T1-1/T1-2（reducer/节点输出校验） |
| 搜索降级 | `app/test/test_search_provider.py` | T1-5～T1-7（DDG 正常/429 注入/缓存） |
| resume | `app/test/test_resume.py` | T3-3/T3-5（崩溃续研/不重跑）+ T4-7（payload 校验） |
| HITL | `app/test/test_hitl.py` | T4-1～T4-9（三 interrupt 点全场景） |
| 记忆 | `app/test/test_memory.py` | T5-2～T5-5（提取/注入/召回/不阻塞） |
| 引用 | `app/test/test_citations.py` | T7-1/T7-2（去重编号/悬挂引用剔除） |

纪律：
- 各 Phase 已"先测试后实现"落了大部分用例；本任务做的是**体系化收口**（补遗漏、进 CI 可一键跑、测试数据 fixture 统一）。
- 需要 LLM 的集成测试统一走 fake_agent/stub 模式（各 Phase 契约测试已示范），CI 无外部依赖可跑；真实 LLM 冒烟单独标记 `@pytest.mark.llm` 本地跑。
- pytest 配置（`pyproject.toml` 或 pytest.ini）：markers（llm/slow/offline）、默认排除 llm。

### P8-2 应用容器化

现状：仅有 `docker-compose.middleware.yml`（PG/Milvus/RabbitMQ/MinIO/Redis 中间件，保留不动）。

**新增**（应用本身，不含新搜索服务）：

```dockerfile
# Dockerfile（后端）
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/  config.json  scripts/ scripts/
# .env 通过 compose environment 注入，不打进镜像
CMD ["uvicorn", "app.app_main:app", "--host", "0.0.0.0", "--port", "8000"]
```

```yaml
# docker-compose.app.yml（新增，与 middleware 分离）
services:
  backend:
    build: .
    ports: ["8000:8000"]
    env_file: .env
    depends_on: [postgres, redis, rabbitmq, milvus, minio]   # 引用 middleware compose（extends 或网络共享）
  frontend:
    build: agent_front/          # 多阶段：node build → nginx 托管
    ports: ["8080:80"]
```

要点：
- duckduckgo-search 为纯 pip 依赖，**编排中无任何搜索服务容器**（v1.2 决策落地确认）。
- 前端镜像多阶段构建（node:20 build → nginx:alpine），nginx 配置 SSE 透传（`proxy_buffering off`）。
- 注意 `app_main.py` 顶部的 NO_PROXY 逻辑在容器内同样生效（镜像内无系统代理，逻辑无害保留）。

### P8-3 工程文档更新

| 动作 | 文件 |
|------|------|
| 更新 | `工程问题与解决方案记录.md`：重构期间解决的问题（NameError 挂起、resume 重跑全图、哈希伪向量、searxng Windows 检出失败——索引文档第三节已详录）逐条沉淀 |
| 归档 | `docs/archive/`：`DeepResearch_HumanInTheLoop_改造计划.md` 等历史规划文档移入（迁移策略第 5 条） |
| 更新 | 项目 README：新架构图、启动方式（compose middleware → app）、目录结构 |
| 收口 | 本目录 `docs/dev/README.md` 索引各 Phase 状态置 ✅；`docs/event-protocol.json` 与代码同步复核 |

## 3. 验收清单

- [ ] pytest 一键全绿（无 llm 标记），覆盖率核心模块（events/state/service）≥ 80%
- [ ] `docker compose -f docker-compose.app.yml up` 一条命令起全栈（依赖 middleware 先起）
- [ ] 前端容器经 nginx 代理 SSE 流式正常（无缓冲聚包）
- [ ] 历史文档归档完成，README 更新，重构记录沉淀
- [ ] 打 tag `p8-done` / `refactor-complete`

## 4. 风险与对策

| 风险 | 对策 |
|------|------|
| 容器内访问宿主中间件（开发期） | compose 网络统一（app 与 middleware 同 network）；或 .env 中间件地址用服务名 |
| weasyprint 在 slim 镜像缺系统库 | Dockerfile 补 `libpango` 等依赖；若 P7 走了降级方案则无此依赖 |
| Milvus/MinIO 镜像在弱网环境拉取慢 | 预先 `docker pull`；或配置镜像加速 |
| 测试补全范围蔓延 | 本 Phase 性质"不阻塞"：按上表矩阵收口即止，不为覆盖率数字补无意义测试 |
