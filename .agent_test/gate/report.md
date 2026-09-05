<!-- .agent_test/gate/report.md — auto-generated, do not edit manually -->
<!-- Generated: 2026-09-05T00:40:00Z -->
<!-- Commit: cfef2381de8c2c0dfca67910615cf01ce4755b91 -->
<!-- Re-generated after P2-2 fix (coder session): P2-2 已闭环，GATE_STATUS 维持 PASSED -->

# Gate Report

GATE_STATUS=PASSED

## 测试结果

| 指标 | 数值 |
|------|------|
| 状态 | PASSED |
| 总用例 | 297（pytest 295 跑通 + 2 跳过）+ 22（vitest 跑通）|
| 通过 | 295（pytest 含 3 例流式契约 + 6 例 checkpointer 契约）+ 22 |
| 失败 | 0 |
| 跳过 | 2（test_p1_smoke：依赖真实 LLM+PG，已知）|
| 耗时 | pytest 9.19s / vitest 2.43s |
| 详细报告 | `.agent_docs/tests/2026-09-04-deepresearch-full-regression.md` |
| 修复执行记录 | `.agent_docs/impl/2026-09-04-fix-streaming-writer.md`、`.agent_docs/impl/2026-09-05-fix-p2-2-checkpointer.md` |

## 审查结果

- 修复闭环已通过 — P0-1 / P0-2 / P1-1 / P1-2 / P2-1 / **P2-2** 全部解除。
- **P2-2 已在真实环境验证**：AsyncPostgresSaver 真实连接 PG，checkpoint 落库 + 重启恢复 + HTTP 端到端读写全部通过。

## 问题详情

### P0 — Critical（全部解除 ✅）

| ID | 问题 | 状态 | 证据 |
|----|------|------|------|
| P0-1 | 流式节点 `writer` 注解被 Optional 阻断 → token 全丢 | ✅ 已修 | `test_stream_contract.py` 3/3；`test_p4` 3 例自然恢复 |
| P0-2 | ChatView.vue `handleNewChat` 自递归 → UI 冻结 | ✅ 已修 | `vue-tsc` 0 error；`vite build` 成功 |

### P1 — Major（全部解除 ✅）

| ID | 问题 | 状态 | 证据 |
|----|------|------|------|
| P1-1 | `agent.ainvoke` await 语法回归 → test_p4 3 例 TypeError | ✅ 已修 | test_p4 3/3 passed |
| P1-2 | AgentTimeline.vue TS2532 编译失败 | ✅ 已修 | `vue-tsc --build` exit 0 |

### P2 — Minor（全部解除 ✅）

| ID | 问题 | 状态 |
|----|------|------|
| P2-1 | Windows ProactorEventLoop 与 psycopg async 不兼容 | ✅ 已确认（`app_main.py` 已设 Selector 策略）|
| P2-2 | PostgresSaver/RedisSaver 同步实现 → 恒降级 InMemorySaver，PG 持久化失效 | ✅ **已修复 + 实机验证**：改用 `AsyncPostgresSaver`，启动日志「使用 PostgreSQL checkpointer（异步）」，checkpoint 落 PG、重启可恢复 |

## 关联交付

- 回归门禁：`app/test/test_stream_contract.py`（3 例）+ `app/test/test_checkpointer.py`（6 例）
- coder 执行记录：
  - `.agent_docs/impl/2026-09-04-fix-streaming-writer.md`（P0/P1）
  - `.agent_docs/impl/2026-09-05-fix-p2-2-checkpointer.md`（P2-2）
- 实机冒烟脚本：`scripts/verify_p2_2_persist.py`
- 变更文件集（P2-2）：
  - `app/mult_agents/runtime.py`（checkpointer 异步单例 + sync 工厂）
  - `app/backend/service/research_service.py`（graph 方法 sync→async）
  - `app/backend/service/task_registry.py`（aget_state）
  - `app/backend/router/research_router.py`（await）
  - `app/app_main.py`（lifespan 集成）

## 已知遗留风险 / 后续建议

| 项 | 说明 |
|----|------|
| E2E 浏览器自动化（E3-E5） | 需前端 + Playwright 全链路，未在本次实机验证范围（本次聚焦 P2-2 后端持久化）|
| `eval_metrics.py` sync 路径 | 仍走 `build_checkpointer`（sync），若未来切 astream 需迁移 |
| checkpointer 连接池 | `AsyncPostgresSaver.from_conn_string` 单连接，高并发可评估 pool |

## 门禁准入

- [x] 全部 P0 / P1 / P2 已闭环
- [x] 契约测试沉淀为门禁（流式 3 + checkpointer 6）
- [x] pytest / vitest 全量绿
- [x] P2-2 实机验证（PG 持久化 + 重启恢复 + HTTP 读写）
- [ ] E2E 浏览器自动化复验（待前端联调触发）
- [ ] reviewer 复检（建议会签）
