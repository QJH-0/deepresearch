# B4 执行记录：检索计划改为「先宽后窄」

- 日期：2026-09-19
- 阶段：方案 B 第 4 步（智能层，B 阶段最后一项）
- 前置：B1（自适应重检）/ B2（工具）/ B3（reflect 并入 analyze）

## 问题：优先级是**反的**

Anthropic 点名的反模式是「agent 默认给出又长又具体的查询，返回结果极少，
正确策略是先宽后窄」。本项目比这更严重 —— `_derive_search_plan` 的排序**完全倒置**：

| 顺序 | 来源 | 形态 |
| --- | --- | --- |
| ①（最优先） | `sub_questions` | **长句**，如「2024年主流AI Agent框架（如LangChain…）在架构设计上呈现出哪些核心发展趋势」 |
| ② | `_derive_direct_search_queries` | 短，围绕原问题 |
| ③（最末） | `outline[].search_queries` | **LLM 专为检索生成的短词** |

而计划上限只有 **6 条**（`deduped[:6]`）。`sub_questions` 有 1 个核心 + 2~3 个扩展，
再叠加 `_derive_direct_search_queries` 的 5 条，**LLM 精心生成的短检索词几乎必然被挤掉**。

实测日志印证：真实运行的检索词全是长句
（`queries=['【核心原问题】2024年主流AI Agent框架（如LangChain、LlamaIndex…`）。

## 改动

### 1. 顺序反转（`_derive_search_plan`）

```
① LLM 的 search_queries（短而宽，最该先跑）
② 围绕原问题的确定性宽查询
③ 子问题压短后兜底（长句检索效果差，只在前面都没产出时用）
```

### 2. 新增 `_condense_query`

- 去 `【…】` / `（…）` 结构性标注、去疑问尾巴（「呈现出/有哪些/是什么…」）
- 超长时**按子句边界截断**（`，、；` 或空格），避免把词切一半
- 上限 40 字
- **只做确定性处理，不做语义提炼** —— 那需要额外一次 LLM 调用，成本与收益不成比例；
  真正的短词由 `plan` 节点产出

### 3. `plan` 提示词（要求 2 改写）

原：「search_queries 必须是针对子问题的**自然语言检索词**」—— 正是这句在鼓励长句。

新：「search_queries 必须是**短检索词**（每条不超过 15 字），用关键词组合而非整句 ——
例如写「LangGraph 状态机」而不是「LangGraph 是如何实现状态机编排的」；
同章节内先给宽泛的词，再给更具体的词」

### 4. 顺带修 `_is_query_grounded` 的过度过滤

顺序反转后暴露：该函数只对**用户原问题**判接地，于是
「LangGraph 状态机」与「2024年AI Agent框架发展趋势调研」无词面重叠 → 被判为幻觉丢弃。

改为对「原问题 + 全部子问题 + **所属章节的标题与描述**」整体判接地：
一条检索词只要能对上它所属章节的主题就合法。

同时保留反幻觉能力：与话题、子问题、所属章节都对不上的词（如
「如何选购冰箱」）仍被挡掉 —— 有测试锁定这一条。

## 效果（确定性验证）

输入：大纲 `sec_1` 的 search_queries = `["LangGraph 状态机", "AutoGen 多智能体协作", "CrewAI 角色编排"]`，
子问题含长句，query = 「2024年AI Agent框架发展趋势调研」

```
1. [sec_1      ] LangGraph 状态机          ← LLM 短词优先
2. [sec_1      ] AutoGen 多智能体协作
3. [sec_1      ] CrewAI 角色编排
4. [user_query ] 2024年AI Agent框架发展趋势调研
5. [user_query ] 框架发展趋势调研是什么
6. [user_query ] 框架发展趋势调研 GitHub
```

`_condense_query` 实测：85 字 → 28 字（`2024年主流AI Agent框架在架构设计与技术演进上`）。

## 验证

| 项 | 结果 |
| --- | --- |
| 后端全量 | **643 passed, 2 skipped** |
| 回归专项 | **116 passed, 7 deselected**（与基线一致） |
| 前端 | **6 文件 71 passed**（与基线一致） |
| **人为漂移验证** | 跳过 outline 检索词 → `test_llm_search_queries_come_first` 与 `test_grounding_accepts_query_matching_its_own_section` **FAILED**；还原后 17 passed |

## 测试侧的一处反转

`test_evidence_queries.py::TestDeriveSearchPlan::test_sub_questions_injected_first`
断言的是**旧顺序**（子问题优先），已按 B4 的新契约重写，并新增：
- 接地判定接受「与所属章节同主题」的检索词
- 反幻觉能力仍在（不相关词被挡）
- `_condense_query` 的标注剥离 / 短词不动 / 子句边界截断

## 待验证

- **端到端效果未验证**：短词是否真的提升 `citation_coverage` 与召回量，需同题对比
- 与 B1/B2/B3 叠加后的整体影响，需一次完整评测（已在跑，`output/eval_b2.log`）

## B 阶段收尾状态

| 项 | 状态 |
| --- | --- |
| B1 自适应检索 | ✅ |
| B2 `fetch_url` 工具 | ✅（真实评测中调用 0 次，需更多样本观察） |
| B3 `reflect` 并入 `analyze` | ✅ |
| B4 查询策略 | ✅ |
| 原计划「消除检索节点里信息增量为零的 LLM 重写」 | ⏸ 未做（需先确认 `rejected_source_ids` / `gaps` / `supports_questions` 的消费方） |
