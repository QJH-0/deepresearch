# 2026-09-05 P2-2 修复执行记录（coder）：checkpointer 异步化 + PG 持久化恢复

> **任务来源**：`.agent_test/gate/report.md` 中 P2-2「PostgresSaver 同步实现 → checkpointer 恒降级 InMemorySaver，PG 持久化实际失效」
> **执行模式**：TDD — 契约测试 red → 实现 green → 全量回归 → 实机验证
> **验证环境**：真实 Windows 开发机，PG(5432)/Redis(6379)/Milvus(19530)/RabbitMQ(5672) 全开 + DashScope LLM

---

## §1 根因

`build_checkpointer()` 里用 `importlib` 导入的是**同步 `PostgresSaver`**，随后检测它是否具备 `aget_tuple/aput/aput_writes` 异步方法——同步 Saver 天然没有这些方法，检测恒为 False → 永远走到「降级 InMemorySaver」分支。

而生产执行链是 `graph.astream()`（异步），langgraph 内部会调用 checkpointer 的 `aget_tuple`/`aput`。同步 `PostgresSaver` 没有这些方法（落到基类 stub 抛 `NotImplementedError`），所以既不能直接用它，也不能靠「升级 3.1.x」解决——**正确解是用 `AsyncPostgresSaver`**。

关键事实（经源码探针确认）：

| Saver | 异步方法 | from_conn_string | setup |
| --- | --- | --- | --- |
| `PostgresSaver`（sync） | 无 aget_tuple/aput | sync contextmanager | sync |
| `AsyncPostgresSaver`（`checkpoint.postgres.aio`） | ✅ 全有 | **async** contextmanager | **async** |
| `AsyncRedisSaver`（`checkpoint.redis`） | ✅ 全有 | async contextmanager | async |

连锁影响：`AsyncPostgresSaver.get_tuple`（sync）在 main thread 会 `raise InvalidStateError`（明确要求用 async 接口）→ 所有 graph 的同步方法（`get_state/get_state_history/update_state`）在 async 上下文中都不可用 → ResearchService 相关方法必须 sync→async。

---

## §2 改动清单

### 2.1 `app/mult_agents/runtime.py`（核心重构）

- 新增 `init_checkpointer(config)`：异步工厂，按 `postgres → redis → memory` 降级链创建 `AsyncPostgresSaver`/`AsyncRedisSaver`，`await ctx.__aenter__()` + `await setup()`，存模块级单例。
- 新增 `close_checkpointer()`：`await ctx.__aexit__()` 关闭连接。
- 新增 `get_checkpointer()`：返回单例（未初始化返回 None）。
- 重写 `build_checkpointer(config)`：**同步工厂**（供 eval_metrics 的 `graph.invoke` 等 sync 场景），直接用 sync `PostgresSaver`/`RedisSaver`，删除了错误的「async 支持检测导致误降级」逻辑。

### 2.2 `app/backend/service/research_service.py`

- `_ensure_initialized`：`get_checkpointer() or build_checkpointer(config)` 复用 lifespan 单例。
- 以下方法 sync→async，内部改用 `aget_state/aget_state_history/aupdate_state`：
  - `get_state`、`get_state_history`、`update_state`、`get_thread_messages`
  - `_trigger_memory_extract_from_snapshot`（及 resume_stream 内两处调用点加 `await`）
  - `_apply_summary_if_needed`、`_apply_summary_to_checkpoint` 内 `get_state→aget_state`、`update_state→aupdate_state`
  - `stream_research`/`resume_stream` 内 `get_state→await aget_state`
- 顺带清理 `get_state` 内一段死代码（`import asyncio ... pass`）。

### 2.3 `app/app_main.py`

- lifespan 启动：`_init_infra()` 后、崩溃恢复扫描前，`await init_checkpointer(config)`。
- lifespan 关闭：`close_store()` 后 `await close_checkpointer()`。
- `_init_task_registry_and_scan`：`get_checkpointer() or build_checkpointer(config)` 复用单例。

### 2.4 `app/backend/service/task_registry.py`

- `scan_orphans`：`graph_app.get_state` → `await graph_app.aget_state`。

### 2.5 `app/backend/router/research_router.py`

- 6 处调用改为 `await`：`get_thread_messages`（3 处）、`get_state`、`get_state_history`、`update_state`。

### 2.6 `app/test/test_checkpointer.py`（新增契约测试，6 例）

覆盖：AsyncPostgresSaver 具备 async 方法、sync PostgresSaver 无 async 方法（对比证据）、init_checkpointer 选 AsyncPostgresSaver、连接失败降级 InMemorySaver、单例语义、build_checkpointer 同步工厂选 sync Saver。

---

## §3 验证矩阵

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 契约测试 | `pytest app/test/test_checkpointer.py -v` | ✅ 6 passed |
| 后端全量 | `pytest app/test/` | ✅ 295 passed, 2 skipped, 0 failed |

## §4 实机验证（真实环境）

### 4.1 独立脚本冒烟（`scripts/verify_p2_2_persist.py`）

```
[1] checkpointer 类型: AsyncPostgresSaver
[2] graph 构建完成, checkpointer 绑定: AsyncPostgresSaver
[3] 研究完成, final 长度: 50
[4] checkpoint 落库, snapshot.values 键: ['messages','clarifications','query',...]
[5] 已关闭 checkpointer（模拟进程重启）
[6] 重启后从 PG 恢复 checkpoint: ✅ 成功
```

### 4.2 后端真实启动 + HTTP 端到端

- 启动日志：`[memory] 使用 PostgreSQL checkpointer（异步）`（此前是「降级到内存」）
- `POST /api/v1/research/run` 真实 LLM 研究 → 返回 `final`，PG `checkpoints` 表落 10 条（含完整 parent→child 链路）
- `GET /api/v1/research/state/{id}` → `has_checkpoint: true`，`aget_state` 异步读取成功
- `GET /api/v1/research/threads/{id}/messages` → 从 PG 读出完整 4 条对话历史

### 4.3 测试数据清理

- 验证产生的 `p2_2_` 前缀 thread 的 checkpoint/writes/blobs 已全部清理，`checkpoints` 表 0 残留。

---

## §5 设计取舍

1. **双轨分离（async 单例 + sync 工厂）**：生产 `astream/ainvoke` 走 `init_checkpointer`（async），eval_metrics 的 `graph.invoke` 走 `build_checkpointer`（sync）。避免用「async 支持检测」一刀切，把 sync 场景也误降级。
2. **单例复用而非每次重建**：checkpointer 在 lifespan 一次性初始化，`ResearchService._ensure_initialized`（懒加载）通过 `get_checkpointer()` 复用，避免每个请求建连接。
3. **sync→async 方法改造是必然代价**：`AsyncPostgresSaver` 的 sync 方法在 main thread 会抛异常，graph 的只读方法（get_state/get_state_history/get_thread_messages）必须 async 化，router 层随之 `await`。
4. **Windows SelectorEventLoop 是前置依赖**：`init_checkpointer` 依赖 `asyncio.WindowsSelectorEventLoopPolicy`（P2-1 已在 `app_main.py` 设置）；独立脚本复验时需同样设置，否则 psycopg async 报 `ProactorEventLoop` 错误。

---

## §6 遗留与后续

- P2-2 已闭环，GATE_STATUS 中该项由「待办」翻转为「已修复」。
- 建议后续：`eval_metrics.py` 仍走 `build_checkpointer`（sync），若未来评估也切 astream，需同步迁移到 `init_checkpointer`。
- 建议后续：`init_checkpointer` 未加连接池（`AsyncPostgresSaver.from_conn_string` 单连接），高并发场景可评估是否需要 pool。
