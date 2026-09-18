# A1 执行记录：修正 State reducer 语义

- 日期：2026-09-18
- 阶段：方案 A 第 1 步（地基）
- 计划来源：`docs/plans/优化开发计划-多智能体架构升级.md` §4 阶段 A1

## 问题

`state.py` 对 15 个字段统一声明 `operator.add`，但节点返回约定不统一，产生两类错误：

1. **累积型字段的节点返回了全量** —— `web_search` 返回 `existing + evidence`，reducer 再拼一次
   → 递推 `e(n+1) = 2·e(n) + new`，每轮迭代证据列表近似翻倍
2. **覆盖型字段被声明成累积** —— `outline` / `evidence_pool` 每轮重建，旧值不丢弃
   → 计划与证据池随迭代轮次线性堆积

最小复现（llmdev 实测）：

```
输入 items=['seed']，节点 return {'items': existing + ['new']}  →  ['seed','seed','new']  ← 重复
对照：节点 return {'items': ['new']}                              →  ['seed','new']         ← 正确
```

最隐蔽的一处：`web_search` 无检索结果时返回 `state.get("web_evidence", [])`，
reducer 执行「旧值 + 旧值」——**「什么都没搜到」反而把证据翻倍**。

## 语义判据

**看字段语义是「历史累积」还是「当前值」**：

| 语义 | reducer | 节点返回约定 |
| --- | --- | --- |
| 历史累积（证据库 / 检索轨迹 / 澄清问答） | `operator.add` | **只返回本轮增量** |
| 当前值（计划 / 每轮重建的派生数据 / 每轮重新分析得出的结论） | 无 | 返回全量，后写覆盖 |

按此判据修正 11 个字段为覆盖语义，4 个累积字段的节点改为只返回增量。

## 改动清单

| 文件 | 改动 |
| --- | --- |
| `app/mult_agents/state.py` | 模块 docstring 重写为 reducer 选用规则；`outline`/`sub_questions`/`research_questions`/`search_plan`/`supplementary_queries`/`evidence_pool`/`audit_flags`/`findings`/`claim_map`/`source_index`/`missing_gaps` 去掉 `operator.add` |
| `app/mult_agents/nodes/web_search.py` | 只收集本轮 trace；早退分支不再回写 `web_evidence`；主返回只给本轮 `evidence` |
| `app/mult_agents/nodes/local_rag.py` | 同上 |
| `app/mult_agents/nodes/write.py` | 见下「连带修复」 |
| `app/test/test_state_semantics.py` | **新增**（7 用例） |
| `app/test/test_p4.py` | 新增 `TestWriteToPlanIntentHandoff`（2 用例） |
| `app/test/test_report_review.py` | 修正 reject 断言：裸字符串 → dict |

## 连带修复：write → plan 的用户意图传递（根因同一处）

改动 `sub_questions` 语义后暴露了一个既有缺陷：`write` 把用户意图写进 plan 读不到的通道。

- `plan_node` **只从 `user_feedback["feedback"]` 读修改意见**（`plan.py:33`，且有 `isinstance(..., dict)` 守卫），
  且会用自己重新生成的子问题**覆盖** `sub_questions`。
- `deepen` 分支把方向写进 `sub_questions` → 被 plan 覆盖 → **用户要求静默丢弃**
- `reject` 分支把 `feedback` 裸字符串写进 `user_feedback` → 被 isinstance 守卫拦掉 → **同样静默丢弃**

两处均改为写入 `user_feedback` 的 dict 形式。`test_report_review.py` 原断言锁的是裸字符串
（即错误契约），已一并修正。

> 归类依据：`deepen` 是我改动的直接后果（必须修）；`reject` 属同类缺陷、同一 match 块、
> 同一「用户意图→plan」通道，且命中治理规则「静默失败发现即改」。

## 验证证据

| 项 | 结果 |
| --- | --- |
| 红阶段（改实现前） | `test_state_semantics.py` **6 failed**；`TestWriteToPlanIntentHandoff` **2 failed** |
| 绿阶段（改实现后） | `test_state_semantics.py` 7 passed；`test_p4.py` 39 passed |
| 后端全量（排除回归文件） | **545 passed, 2 skipped**（基线 536 → +9 新增） |
| 回归专项（deselect 3 个需 MQ 的类） | **116 passed, 7 deselected**（与基线一致） |
| **人为漂移验证** | 把 `web_search` 改回 `state.get("web_evidence", []) + evidence` → `test_returns_only_new_evidence` **FAILED**；还原后 7 passed |

漂移验证证明断言不是「永远为绿」。

## 未处理 / 遗留

- 检查点兼容：按用户决策，升级时清理开发环境 checkpoint 表，不写迁移逻辑
- 阶段 A2（清死字段）与 A3（上下文层）尚未开始
