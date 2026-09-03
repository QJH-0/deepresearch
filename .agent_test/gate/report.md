<!-- .agent_test/gate/report.md — auto-generated, do not edit manually -->
<!-- Generated: 2026-09-03T09:05:00Z -->
<!-- Commit: 069d730a11d638aa6c5b12bd0eb390fa2d47c646 -->

# Gate Report

GATE_STATUS=PASSED

## 测试结果

| 指标 | 数值 |
|------|------|
| 状态 | PASSED |
| 总用例 | 103 |
| 通过 | 103 ✅ |
| 失败 | 0 ❌ |
| 跳过 | 0 ⏭️ |
| 耗时 | 1.23s |

### 测试分项

| 测试套件 | 用例数 | 通过 | 失败 | 耗时 |
|----------|--------|------|------|------|
| test_integration_audit.py (新增联调测试) | 45 | 45 | 0 | 0.38s |
| test_hitl.py (HITL 回归) | 26 | 26 | 0 | 0.30s |
| test_stream_events.py (事件协议回归) | 14 | 14 | 0 | 0.30s |
| test_resume.py (断点续研回归) | 18 | 18 | 0 | 0.25s |

## 审查结果

待审查

## 问题详情

无失败或问题。

## 修复摘要

本次审计修复覆盖以下问题：

### P0 — 阻断级（已修复）

| ID | 问题 | 修复方式 |
|----|------|----------|
| P0-1 | HITL resume payload 缺 kind 字段 | 前端三卡片(PlanApprovalCard/ClarifyCard/ReportReviewCard) emit 统一补 `kind` 字段 |
| P0-2 | analyze.py 用原始 interrupt() + type 字段 | 改用 `_shared.raise_interrupt("clarification", ...)` 统一封装 |
| P0-3 | 旧 workflow_service.py 未删除 | 删除文件，路由全部迁移到 ResearchService，清理 __init__.py 和 test_p1_smoke.py |
| P0-4 | 前端回滚字段与后端不匹配 | fetchHistory 适配 `{history:[{checkpoint_id,...}]}`，rollbackThread 改发 `{thread_id, values:{checkpoint_id}}`，RollbackMenu.vue 同步适配 |

### P1 — 重要（已修复）

| ID | 问题 | 修复方式 |
|----|------|----------|
| P1-3 | interrupt store 缺重建逻辑 | 新增 `rebuild(threadId)` 方法，调 `GET /threads/{id}/interrupt` 重建 |
| P1-4 | ReportReviewCard 动作枚举不一致 | `accept`→`adopt`，删除后端未定义的 `reject` |

### P2 — 次要（已修复）

| ID | 问题 | 修复方式 |
|----|------|----------|
| P2-1 | weasyprint 未收录 | requirements.txt 补 `weasyprint>=62.0` |
| P2-6 | 旧 sse.ts 死代码 | 删除 `agent_front/src/utils/sse.ts` |

### 环境状态

- Docker middleware: ✅ 已启动 (Redis/PostgreSQL/Milvus/RabbitMQ/etcd/MinIO/MySQL/Neo4j)
- Git commit: `069d730` on `refactor/main`
