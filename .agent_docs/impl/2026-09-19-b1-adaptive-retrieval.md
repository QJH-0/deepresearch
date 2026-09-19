# B1 执行记录：检索阶段的自适应重检循环

- 日期：2026-09-19
- 阶段：方案 B 第 1 步（智能层）
- 计划来源：`docs/plans/优化开发计划-多智能体架构升级.md` §4 阶段 B1
- 前置：A 阶段（A1/A2/A3）与评估设施已完成

## 目标

补上 Agentic RAG 的「结果验证」环节：检索不再固定一轮，而是由裁判判定够不够，
不够就用**针对缺口的新检索词**再检一轮。

## 与计划的偏差：不合并子图，改为平铺节点

计划原文是「把 `web_search` + `local_rag` 合并为 `retrieve` 子图，节点数 10 → 9」。
**实际改为平铺新增一个 `retrieve_grader` 节点**，节点数 10 → 11。理由：

1. **子图有状态合并风险。** LangGraph 子图结束时会把子图 state 作为 update 应用到父图，
   经父图的 reducer 合并。而 `web_evidence`/`local_evidence` 正是 `operator.add` 语义——
   子图内部已累加过的证据会被父图再累加一次，**恰好复现 A1 刚修掉的重复累加**。
2. **节点数不是目标。** 计划里「10 → 9」是副产品；真正的收益是「自适应检索深度」，
   平铺方案完整拿到了。
3. **复用已验证的节点代码。** 合并子图要重写两个已充分测试的检索节点，
   收益不成比例。

原计划的另一个目标「消除检索节点里信息增量为零的 LLM 重写」**本次未做**——
它需要先确认 `rejected_source_ids` / `gaps` / `supports_questions` 的消费方，
属独立改动，留待后续。

## 拓扑

```
plan →(web_search ∥ local_rag) → retrieve_grader
retrieve_grader →(不充分且未达上限)→ 回 (web_search ∥ local_rag)
retrieve_grader →(充分 / 达上限 / 无补检词)→ deep_dive → analyze
analyze →(reflect ↺ | write)
reflect →(web_search ∥ local_rag)
```

两级循环，层级不同不混用：

| 循环 | 判定者 | 问题 |
| --- | --- | --- |
| **内层**（新增） | `retrieve_grader` | 这一轮检索够不够？不够就换词再搜 |
| **外层**（原有） | `reflect` | 跨轮次还要不要继续研究？ |

`reflect` 的补搜同样经过 grader，所以新一轮检索也能自适应加深。

## 改动清单

| 文件 | 改动 |
| --- | --- |
| `output_schemas.py` | 新增 `RetrievalGradeDraft`（sufficient / gaps / rewritten_queries） |
| `prompts.py` | 新增 `retrieve_grader` 提示词（不规定输出格式，格式由 schema 承担） |
| `models.py` | `build_agents` 增加 `retrieval_grader`（温度 0.0，判定需可复现） |
| `runtime.py` | `AgentBundle` 增加 `retrieval_grader` 字段 |
| `nodes/retrieve_grader.py` | **新增**：裁判节点 |
| `nodes/__init__.py` | 导出 `retrieval_grader_node` |
| `nodes/_evidence.py` | `_build_queries` 优先级改为「内层重检词 > 外层补搜计划 > 首轮计划」 |
| `graph.py` | 拓扑变更 + `route_after_retrieval_grade` |
| `state.py` | `retrieval_grade` / `retrieval_queries`（当前值）+ `retrieval_round` / `max_retrieval_rounds`（ProgressState） |
| `config.json` / `Settings` / `AppConfig` | 新增 `max_retrieval_rounds: 2` |
| `backend/service/research_service.py` | 4 处 `create_initial_state` 传入配置值 |
| `test_retrieve_grader.py` | **新增**：9 用例 |
| `test_p1.py` | 节点集合加 grader；**新增拓扑边锁定测试**；**删掉吞异常的兜底** |
| `test_structured_output.py` | 结构化节点映射 7 → 8 |
| `test_state.py` / `test_state_semantics.py` / `test_p4.py` | 新字段纳入断言 |

## 三条防死循环 / 防静默降级的规则

节点里每个分支都对应一条硬规则，均有测试锁定：

| 分支 | 规则 | 不做会怎样 |
| --- | --- | --- |
| 充分 → 放行 | 轮次归零 + 清空补检词 | 下一轮外层研究会被上一轮的补检词污染 |
| 不充分 + 有词 + 未达上限 → 重检 | 三条同时满足才重检 | 缺「有词」会拿同样的词空转，白烧配额 |
| 不充分 + **已达上限** → 放行 | 硬闸 | 死循环 |
| 不充分 + **无补检词** → 放行 | 硬闸 | 同上 |
| 结构化失败 → 放行 + `degraded=True` | 留痕 | 自适应能力被静默关闭，没人知道 |

**降级为什么选「放行」而非「重试」**：重检需要新的检索词，而检索词正是裁判的产出——
裁判都失败了，循环没有可用输入，继续转只会空烧检索配额。

## 两个被踩出来的坑（都是真问题）

### 1. LangGraph 的条件边不支持「一个标签映射到多个节点」

最初写成：

```python
workflow.add_conditional_edges("retrieve_grader", route, {
    "retrieve": ["web_search", "local_rag"],   # ← 编译期报错
    "continue": "deep_dive",
})
```

编译时报 `TypeError: unhashable type: 'list'`（`compile()` 校验时 `end not in self.nodes`
拿 list 去 hash）。

**正确写法**：`path_map` 传**节点名列表**，路由函数返回**节点名列表**实现扇出：

```python
workflow.add_conditional_edges(
    "retrieve_grader", route_after_retrieval_grade,
    ["web_search", "local_rag", "deep_dive"],
)
# 路由函数：return ["web_search", "local_rag"] 即扇出到两条边
```

已用最小复现验证：`get_graph().edges` 会出现 `grader→web_search`、`grader→local_rag`
两条边，执行时两个节点确实并行跑。

### 2. `test_p1.py` 的兜底吞掉了这个编译失败

该用例原本是：

```python
try:
    app = build_app(mock_agents, InMemorySaver())
    node_names = set(app.get_graph().nodes.keys())
except (TypeError, Exception):          # ← 捕获一切
    # 退化成「检查 graph.py 源码里出现过节点名」
    ...
    return
```

于是 `build_app` 真的编译失败时，测试**依然通过**——它去源码里 grep 节点名了。
这正是「降级分支把失败伪装成正常」的典型。

**已删除兜底**：图编译失败必须让测试失败。同时新增 `test_retrieval_inner_loop_topology`
锁定边集合（此前**没有任何测试锁定边**，拓扑改动只能靠人看 diff）。

### 3. dataclass 字段顺序

`AppConfig` 是 `@dataclass(frozen=True)`。我把 `max_retrieval_rounds: int = 2`（带默认值）
插在了 `max_iterations`（无默认值）之后，触发
`TypeError: non-default argument 'enable_memory' follows default argument`，
3 个测试文件收集期即失败。

**修法**：移到有默认值的区段（`thinking_nodes` 之后），并注明位置受 dataclass 规则约束。

## 验证

| 项 | 结果 |
| --- | --- |
| 新增测试 | `test_retrieve_grader.py` 9 用例；`test_p1.py` 拓扑边锁定 1 例 |
| 后端全量 | **616 passed, 2 skipped**（B1 前 606 → +10） |
| 回归专项 | **116 passed, 7 deselected**（与基线一致） |
| **人为漂移验证** | 去掉 `round_index < max_rounds` 上限闸 → `test_stops_at_max_rounds` **FAILED**；还原后 9 passed |
| 图编译 | `build_app` 编译通过；边集合断言含 `grader→web_search` / `grader→local_rag` / `grader→deep_dive` |

## 待验证

- **端到端效果未验证**：自适应重检是否真的提升了引用覆盖率 / 完备性，
  需要「B1 前 / B1 后」同题对比。已启动一次端到端运行（`output/eval_b1.log`）。
- **成本影响未知**：每轮检索多一次裁判 LLM 调用；触发重检时还会多一轮检索。
  需与 `citation_coverage` 的提升幅度一起看是否划算。
