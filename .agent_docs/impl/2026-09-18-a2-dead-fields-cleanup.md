# A2 执行记录：清理死字段与 thinking_nodes 不一致

- 日期：2026-09-18
- 阶段：方案 A 第 2 步
- 计划来源：`docs/plans/优化开发计划-多智能体架构升级.md` §4 阶段 A2
- 前置：A1 已完成（`2026-09-18-a1-state-reducer-semantics.md`）

## 问题

State 中 5 个字段只写不读：节点每轮产出，无人消费，却让 checkpoint 体积与调试噪音持续增长。
其中 `web_search` / `local_rag` / `deep_dive` 与**图节点同名**，尤其容易误导排查
（grep 命中的多是节点名或 SSE 标签映射，而非 State 字段）。

另有一处配置三方不一致：`thinking_nodes` 在 `config.json` 为 `[]`，而代码默认值与
`config.py` 的 `getattr` 回退值都是三个节点。当前因 `config.json` 显式提供了键而不生效，
但一旦该键被移除，深度思考会**静默重新打开**——只付延迟与成本（实测 write 77.3s→42.9s）。

## 删除前的读取方核实

逐字段 grep 非测试代码，确认命中的全是同名但不同物：

| 字段 | grep 命中 | 判定 |
| --- | --- | --- |
| `code` | `report_check.py:58`（CheckItem 数据类字段） | 同名不同物，State 字段无读取方 |
| `web_search` | `graph.py` 节点注册、`models.py` 的 `_structured_for("web_search")`、`research_service.py:127` 节点标签映射、`prompts.py` system prompt 键 | 均为**节点名**，非 State 字段 |
| `local_rag` | 同上（`:128`） | 同上 |
| `deep_dive` | `graph.py`、`config.py`/`settings.py` 的 thinking_nodes、`:129`、`prompts.py` | 同上 |
| `audit` | 仅 `deep_dive.py:100`（写）与 `state.py`（初始化） | 无读取方 |

## 改动清单

| 文件 | 改动 |
| --- | --- |
| `app/mult_agents/state.py` | ResearchState 与 `create_initial_state` 删除 `code`/`web_search`/`local_rag`/`deep_dive`/`audit` |
| `app/mult_agents/nodes/web_search.py` | 两个 return 去掉 `web_search` 键 |
| `app/mult_agents/nodes/local_rag.py` | 两个 return 去掉 `local_rag` 键 |
| `app/mult_agents/nodes/deep_dive.py` | return 去掉 `deep_dive` 与 `audit`（本就写同一个值） |
| `app/mult_agents/config.py` | `thinking_nodes` 默认值与 `getattr` 回退值 → `[]` |
| `app/backend/config/settings.py` | `thinking_nodes` 默认值 → `[]` |
| `app/test/test_state.py` | `REQUIRED_FIELDS` 移除 5 字段；新增 `REMOVED_FIELDS` + `test_removed_dead_fields_absent`；两个 reducer 断言从「源码字符串 grep」改为「`__annotations__` 元数据检查」 |
| `app/test/test_p4.py` | `_make_initial_state` 同步移除 5 键 |
| `app/test/test_thinking_stream.py` | `test_default_thinking_nodes` 断言由「三节点在内」改为 `== []` |

### 保留的字段（用户决策）

- `budget`：plan 的 HITL 载荷（`plan.py:79`）会展示给用户
- `research_questions`：报告附录「研究问题」章节展示（`_fallbacks.py:330,362`）

### 顺带加固：把弱断言换成结构断言

原 `test_sources_reducer_uses_operator_add` 只断言源码里出现过 `operator.add` ——
正是这种弱检查放过了 A1 的重复累加。已改为按 `__annotations__` 的 `__metadata__`
逐个字段校验 reducer，累积型字段漏声明 reducer 会立即失败。

## 验证证据

| 项 | 结果 |
| --- | --- |
| 红阶段 | `test_removed_dead_fields_absent` **FAILED**：`assert not {'audit','code','deep_dive','local_rag','web_search'}` |
| 后端全量（排除回归文件） | **546 passed, 2 skipped** |
| 回归专项（deselect 3 个需 MQ 的类） | **116 passed, 7 deselected**（与基线一致） |
| 导出/引用门禁专项（`-k "report or export or pdf or citation"`） | **93 passed** |
| 残留引用检查 | `state["<已删字段>"]` / `state.get("<已删字段>")` 全库零命中 |
| 编译检查 | `compileall app/mult_agents app/backend` 通过 |

## 决策记录

**`DeepDiveDraft.summary` 保留，不删。**
删除 `deep_dive`/`audit` 两个 State 字段后，该 schema 字段已无下游消费者。仍保留的理由：
它是 LLM 在产出结构化裁判结果前的自述（scratchpad 作用），可能对审计质量有正向影响；
且它不进入 State，不产生 checkpoint 存储开销，仅消耗少量输出 token。
**如需进一步压成本，这是下一个可评估的裁剪点。**

## 未处理 / 遗留

- 检查点兼容：按用户决策，升级时清理开发环境 checkpoint 表，不写迁移逻辑
- 阶段 A3（上下文层）尚未开始
