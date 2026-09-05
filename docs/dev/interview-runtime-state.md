# LangGraph State 管理与 Runtime 架构 — 面试准备

> 核查日期 2026-09-04

## 一、速记表

| 技术点/功能点 | 核心方案 | 关键指标 |
|---|---|---|
| State 分组 | TypedDict 多重继承（ConversationState + ResearchState + ProgressState） | 3 组 ~40 字段 |
| State reducer | `add_messages`（对话流）+ `operator.add`（证据/计划累加） | 2 种 reducer |
| 初始 State 工厂 | `create_initial_state()` 全字段初始化 + HITL 配置注入 | 1 个入口函数 |
| AgentBundle | `@dataclass(frozen=True)` 聚合 8 个节点 Agent 实例 | 8 个角色 |
| 模型工厂 | `build_agents()` 按 config.json `node_models` 为每节点配模型 | 节点级温度差异化 |
| Checkpointer 降级链 | PostgreSQL → Redis → InMemorySaver 三级降级 | 3 级 |
| 图拓扑 | `StateGraph(AgentState)` + 条件路由 + 并行边 + 回环 | 10 节点 11 边 |
| HITL interrupt | 3 点中断（clarification / plan_approval / report_review） | 3 种 kind |
| 流式输出 | `graph.astream(stream_mode=["custom","updates"])` + async generator | token 级 |
| 任务注册 | `TaskRegistry` thread_id → asyncio.Task + Redis 兜底 | 单进程权威 |
| 断点续研 | `astream(None, config)` 续跑 / `Command(resume=...)` 回答中断 | 2 种 mode |

## 二、面试口述稿

### 1 分钟极简版

我的 DeepResearch 项目基于 LangGraph 搭建了一个多智能体深度研报系统。State 部分我用 TypedDict 多重继承做了三组分组——对话流、研究数据、进度追踪，其中 messages 用 `add_messages` reducer 做增量追加，证据池和计划列表用 `operator.add` 做累加去重。Runtime 部分我设计了一个 `AgentBundle` frozen dataclass 聚合 8 个节点级 Agent 实例，通过 `build_agents` 模型工厂按 config.json 为每个节点配置不同温度和模型。Checkpointer 做了 PostgreSQL → Redis → 内存三级降级链。图拓扑是 `intent → clarify → plan → [web_search ∥ local_rag] → deep_dive → analyze → (reflect|write) → END`，支持 3 点 HITL interrupt 和 reflect 回环。服务层用纯 async generator + `graph.astream` 实现 token 级 SSE 流式，`TaskRegistry` 管理并发任务和崩溃恢复。

### 3 分钟完整版

**State 设计（约 40 秒）**
原来是一个 41 字段的扁平 TypedDict，只有 messages 有 reducer，其余字段节点写错键就被静默丢弃，排查极难。我把它重构为三个分组：ConversationState 管对话流和澄清记录，ResearchState 管查询、计划、证据池、报告等核心研究数据，ProgressState 管迭代轮次。AgentState 多重继承三者合并。messages 用 LangGraph 的 `add_messages` reducer 支持增量追加和覆盖，证据池、子问题列表、审计标记等用 `operator.add` 做累加，去重逻辑在节点内实现。`create_initial_state()` 工厂函数一次性初始化全部 ~40 个字段，包括 HITL 开关和防死循环的 `plan_revision_count` 计数器。

**Runtime 架构（约 40 秒）**
Runtime 层的核心是 `AgentBundle`——一个 frozen dataclass，聚合了 intent_router、planner、scout_web、scout_local、evidence_judge、analyst、direct_responder、writer 共 8 个 Agent 实例。每个 Agent 通过 `build_agent()` 构建，用 ChatTongyi（阿里通义千问）作为 LLM，绑定各自的 system prompt 和温度。`build_agents()` 工厂函数支持从 config.json 的 `node_models` 字段按节点配置不同模型——比如 plan 节点用 qwen-plus 温度 0.3，intent_router 温度 0.0 保证路由稳定。Checkpointer 做了三级降级：优先 PostgreSQL（`langgraph-checkpoint-postgres`），不可用时降级 Redis（依赖 RediSearch），再不可用降级内存。

**图拓扑与 HITL（约 40 秒）**
图拓扑用 `StateGraph(AgentState)` 构建，核心路径是 `START → intent → (direct_answer | clarify → plan) → [web_search ∥ local_rag] → deep_dive → analyze → (reflect | write) → END`。web_search 和 local_rag 是并行边，reflect 回环到检索节点做补搜。HITL 在三个点用 LangGraph 的 `interrupt()` 实现：clarification（问题澄清）、plan_approval（计划审批，支持 approve/revise/reject 三分支，revise 有轮次上限 3 防死循环）、report_review（报告审核，支持 adopt/deepen）。中断时节点返回 `Command(goto=..., update=...)` 控制流转。

**服务层流式与任务管理（约 40 秒）**
服务层 `ResearchService` 用纯 async generator 替代了旧的 Thread+Queue 桥接，直接 `yield sse(event(...))` 挂到 FastAPI 的 StreamingResponse。`graph.astream(stream_mode=["custom","updates"])` 同时消费节点内 StreamWriter 发出的自定义事件和状态更新事件。token 级流式在 direct_answer 和 write 节点用 `agent.astream(stream_mode="messages")` 实现。`TaskRegistry` 维护 thread_id → asyncio.Task 映射，同一 thread 并发 /run 直接 409，cancel 用 `task.cancel()` 在 await 点抛 CancelledError 真正中断 LLM 调用。断点续研两种模式：`mode=continue` 用 `astream(None, config)` 从最后 checkpoint 续跑，`mode=answer` 用 `Command(resume=resume_value)` 从 interrupt 点继续。

---

## 三、技术点：LangGraph State 分组与 Reducer

> 版本基准：LangGraph v1.0.3 · LangChain v1.0.7 · 核查日期 2026-09-04
> 项目场景：DeepResearch 多智能体研报系统，10 节点 StateGraph，~40 字段 State

### 痛点 → 方案 → 效果

**原始痛点**：旧 State 是一个 41 字段的扁平 TypedDict，只有 `messages` 字段有 `add_messages` reducer，其余字段节点返回 dict 时写错键名就被 LangGraph 静默丢弃——比如旧代码 `nodes.py:1010` 写了 `analysis_summary` 但 State 中没有这个字段，数据直接消失无报错；`nodes.py:622,646` 引用了不存在的 `hypotheses` 字段，恒为 KeyError/None。调试时无法定位是节点逻辑错还是 State 键名不匹配。

**核心方案**：三组分组 + 双 reducer 策略。

```python
class ConversationState(TypedDict):
    messages: Annotated[list, add_messages]       # 增量追加/覆盖
    clarifications: Annotated[list, operator.add] # 累加
    conversation_summary: str

class ResearchState(TypedDict):
    query: str
    intent: str  # direct | multiagent
    outline: Annotated[list[dict], operator.add]
    sub_questions: Annotated[list[str], operator.add]
    web_evidence: Annotated[list[dict], operator.add]
    local_evidence: Annotated[list[dict], operator.add]
    evidence_pool: Annotated[list[dict], operator.add]
    findings: Annotated[list[dict], operator.add]
    # ... ~30 个字段

class ProgressState(TypedDict):
    phase: str
    iteration: int
    max_iterations: int

class AgentState(ConversationState, ResearchState, ProgressState):
    """多重继承合并所有键，等价于扁平 dict，分组仅用于代码组织。"""
```

**为什么这样设计**：
1. `add_messages` reducer：LangGraph 内置的 reducer，支持按消息 ID 增量追加和同 ID 覆盖。对话流需要保留完整历史供 checkpoint 回放和记忆提取，不能用 `operator.add`（会简单拼接不处理 ID 去重）。
2. `operator.add` reducer：证据池、子问题列表、审计标记等是累加型数据——web_search 和 local_rag 并行执行各自返回增量证据，LangGraph 自动用 `operator.add` 合并两个节点的输出。去重逻辑（`_dedupe_sources` 按 url/title 去重）放在节点内部而非 reducer 层，因为去重规则与业务语义强相关。
3. TypedDict 多重继承而非 pydantic BaseModel：LangGraph 的 `StateGraph` 原生支持 TypedDict，运行时合并所有键为扁平 dict，分组不增加运行时开销，仅提升代码可读性。
4. `create_initial_state()` 工厂函数：一次性初始化全部字段（包括 `hitl_enabled=False`、`plan_revision_count=0` 等默认值），避免节点读取未初始化字段导致 None/KeyError。

**效果量化对照表**：

| 指标 | 优化前（扁平 State） | 优化后（分组+reducer） |
|---|---|---|
| 字段静默丢弃 | 写错键名零报错 | reducer 覆盖全部累加字段，未声明键开发期可校验 |
| 证据合并正确性 | 并行节点输出互相覆盖（后写胜出） | `operator.add` 自动累加 |
| 代码可维护性 | 41 字段无分组，查找困难 | 3 组按语义分区 |
| 初始化完整性 | 节点各自判空 fallback | 工厂函数统一初始化 |

**一句话总结**：State 用 TypedDict 三组分组 + `add_messages`/`operator.add` 双 reducer，保证并行节点输出自动累加、对话流 ID 级去重，工厂函数统一初始化消除字段缺失风险。

### 七维提问（含追问链与具体答案）

#### 1. 概念理解

**Q1: LangGraph 的 State reducer 是什么？为什么需要它？**
A: LangGraph 是基于图的状态机框架，每个节点执行后返回一个 dict（部分 State 更新），LangGraph 需要将这个更新合并到当前 State 中。reducer 就是定义"如何合并"的函数。如果没有 reducer，默认行为是后写覆盖（last-write-wins），这在并行节点场景下会丢数据——比如 web_search 和 local_rag 同时返回 `{"evidence": [...]}`，没有 reducer 则后完成的节点覆盖先完成的。`operator.add` 让两个列表自动拼接。

**Q2: `add_messages` 和 `operator.add` 有什么区别？**
A: `add_messages` 是 LangGraph 专门为消息列表设计的 reducer，按消息 ID 去重——同 ID 的消息覆盖（用于编辑/撤回），不同 ID 的追加。`operator.add` 是 Python 标准库的加法操作符，对列表就是 `list1 + list2` 简单拼接，不做去重。对话流需要 ID 级管理（checkpoint 回放、记忆提取需完整消息序列），所以用 `add_messages`；证据池/子问题列表是累加型，去重规则与业务强相关（按 url 去重 vs 按 source_id 去重），所以用 `operator.add` + 节点内自定义去重。

#### 2. 核心原理

**Q3: TypedDict 多重继承在运行时怎么工作？为什么说"等价于扁平 dict"？**
A: TypedDict 是 Python 的类型工具，在运行时其实就是普通 dict——`TypedDict.__annotations__` 在多重继承时合并所有父类的注解键，但实际数据结构是扁平的 dict。LangGraph 读取 `AgentState.__annotations__` 获取全部字段及其 reducer 注解（`Annotated[T, reducer]` 的第二个元素），然后为每个字段构建 channel。分组纯粹是代码组织层面的可读性提升，不影响 LangGraph 的 channel 构建行为。

**追问链：LangGraph 怎么从 `Annotated[list, operator.add]` 提取 reducer？**
→ LangGraph 在 `StateGraph` 初始化时遍历 State 类的 `__annotations__`，对每个 `Annotated` 类型用 `get_type_hints` 解包，取第二个元素作为 reducer 函数。如果字段没有 `Annotated` 包装，默认 reducer 是"覆盖"（直接赋值）。所以标量字段（`query: str`、`iteration: int`）不需要 reducer，每个节点返回时直接覆盖；列表字段必须指定 reducer 否则并行节点会互相覆盖。

#### 3. 项目实战

**Q4: 你项目里 web_search 和 local_rag 是并行执行的，它们的输出怎么合并到 State？**
A: 在 `graph.py` 中，plan 节点之后同时 `add_edge("plan", "web_search")` 和 `add_edge("plan", "local_rag")`，LangGraph 在运行时并行调度这两个节点。web_search 返回 `{"web_evidence": [新证据...], "web_search_trace": [...]}`，local_rag 返回 `{"local_evidence": [新证据...]}`。由于 `web_evidence` 和 `local_evidence` 都是 `Annotated[list[dict], operator.add]`，LangGraph 自动将两个节点的输出列表合并到 State 中。两个节点写的是不同的 State 键（`web_evidence` vs `local_evidence`），所以不存在冲突。如果是同一个键，`operator.add` 会拼接。

**追问链：如果两个并行节点写同一个键，reducer 怎么保证顺序？**
→ `operator.add` 的执行顺序与节点完成顺序一致，但不保证确定性顺序（取决于协程调度）。如果需要确定性，要么用不同的键（如我们的 `web_evidence`/`local_evidence`），要么在后续节点（如 `deep_dive`）中做排序/去重。我们的 `deep_dive_node` 读取 `web_evidence + local_evidence` 合并后做统一评分和去重。

#### 4. 配置 API

**Q5: `create_initial_state()` 的参数和默认值是怎么设计的？**
A: 函数签名是 `create_initial_state(query, max_iterations, user_id, tenant_id, memory_context="", hitl_enabled=False, hitl_config=None) -> AgentState`。必填参数是 query/max_iterations/user_id/tenant_id（运行必需），可选参数有默认值。`hitl_config` 默认 `{"plan_review": True, "analyze_clarify": True, "write_review": False}`——plan 审批和 analyze 澄清默认开启，报告审核默认关闭（减少用户交互摩擦）。返回值是一个扁平 dict，包含全部 ~40 个字段的初始值。

**追问链：HITL 配置怎么在运行时动态开关？**
→ `ResearchService._build_runtime_config()` 用 `AppConfig.with_overrides(hitl_enabled=...)` 在每次请求时覆盖配置，传入 `create_initial_state()`。前端通过 API 参数 `hitl_enabled=true` 控制。节点内部通过 `state.get("hitl_enabled", False)` 和 `state.get("hitl_config", {}).get("plan_review", True)` 两层判断决定是否触发 interrupt。

#### 5. 故障调优

**Q6: 如果节点返回了 State 中不存在的键，会发生什么？**
A: LangGraph 1.x 对此的行为是：如果返回的键不在 State 定义中，该键值对被静默忽略（不报错但不写入 State）。这正是旧代码的痛点——`analysis_summary` 写了但不生效。我们的改进策略是：① 将所有节点实际输出的字段都定义到 State 中（如 `analysis` 字段）；② 在开发期用契约测试（`test_state.py` 的 `TestCreateInitialState` 验证所有必需字段存在）防止遗漏；③ 代码审查时对照 State 定义检查节点返回 dict 的键。

**追问链：你怎么排查"节点输出被静默丢弃"的问题？**
→ 三步排查法：① 在节点函数末尾 `logger.info` 打印返回的 dict 键列表；② 在下一个节点入口打印 `state.get(key)` 确认值是否存在；③ 如果不存在，grep State 定义确认键名是否匹配。根本性修复是在 `create_initial_state` 中预声明所有字段 + 节点返回 dict 的键集 ⊆ State 字段集。

#### 6. 对比选型

**Q7: 为什么不用 pydantic BaseModel 做 State，而用 TypedDict？**
A: 两个原因：① LangGraph 原生支持 TypedDict 作为 StateGraph 的泛型参数，`StateGraph(AgentState)` 直接工作；如果用 pydantic BaseModel 需要额外适配层或用 LangGraph 的 pydantic State 支持特性（1.x 已支持但成熟度不如 TypedDict）；② TypedDict 运行时是普通 dict，序列化/checkpoint 序列化天然兼容（PostgresSaver 直接序列化 dict）；pydantic BaseModel 需要 `.model_dump()` 转换。TypedDict 的类型检查在 mypy/pyright 静态分析阶段完成，运行时零开销。

**追问链：如果需要运行时校验节点输出（如字段类型/必填校验），TypedDict 怎么办？**
→ 方案是在节点输出处用 pydantic 模型做显式校验（如 `PlanOutput.model_validate(out)`），但 State 定义本身用 TypedDict。这分离了"State 合并语义"（LangGraph 管）和"输出校验"（pydantic 管）。我们的项目中目前用契约测试代替运行时校验——`test_state.py` 验证 `create_initial_state` 返回所有必需字段，`test_resume.py` 验证 resume payload 结构。

#### 7. 延伸深挖

**Q8: 如果 State 字段数量继续增长（比如 100+），分组和 reducer 策略怎么扩展？**
A: 三个方向：① 进一步细分分组——从 3 组扩展到 5-6 组（如拆出 `EvidenceState`、`ReportState`），每组字段数控制在 15 以内，每组有明确的语义边界；② 引入 Channel 概念——LangGraph 支持 `add_channel()` 定义更复杂的 channel 类型（如自定义 reducer），对于需要复杂合并逻辑的字段（如带优先级合并的证据池）可用自定义 reducer 替代 `operator.add`；③ 评估是否所有字段都需要放 State——纯节点内部使用的中间态（如原始搜索记录 `_summarize_records` 的返回值）可以放局部变量，只有跨节点消费的数据才放 State。我们当前已有这个实践：`raw_records` 在 web_search 节点内是局部变量，只有清洗后的 `evidence` 写入 State。

**追问链：自定义 reducer 怎么实现？**
→ 定义一个签名为 `(existing: T, new: T) -> T` 的函数，在 `Annotated[list[dict], my_custom_reducer]` 中使用。例如可以实现一个"按 source_id 去重并保留最高分"的证据 reducer：`def evidence_reducer(existing, new): merge by source_id, keep highest score`。但我们选择在节点内做去重，因为去重规则可能随业务演进变化，放节点内更灵活。

### 反向选型题

**Q: 为什么不用 LangGraph 的 `MessagesState` 预置 State 替代手写 ConversationState？**
A: `MessagesState` 是 LangGraph 提供的预置 State，只包含 `messages: Annotated[list, add_messages]` 一个字段。不用的原因：① 我们的对话流还需要 `clarifications`（澄清问答记录）和 `conversation_summary`（摘要压缩文本），这些不是消息而是独立的 State 字段，需要自己的 reducer（`operator.add`）和标量类型；② `MessagesState` 的设计哲学是"State = 消息列表"，但我们的是"State = 研究数据 + 对话流 + 进度"，消息只是其中一小部分。继承 `MessagesState` 再扩展也能工作，但不如直接定义 `ConversationState` 清晰。

### 行业最佳实践对照

| 业界方案 | 本项目实现 | 状态 |
|---|---|---|
| State 分组（open_deep_research 模式） | TypedDict 三组多重继承 | ✅ 已做到 |
| 列表字段指定 reducer | `operator.add` + 节点内去重 | ✅ 已做到 |
| 消息流用 `add_messages` | messages 字段 | ✅ 已做到 |
| 初始 State 工厂函数 | `create_initial_state()` | ✅ 已做到 |
| 节点输出 pydantic 运行时校验 | 用契约测试代替 | ⚠️ 未做到（测试覆盖但无运行时校验） |
| 自定义复杂 reducer（如优先级合并） | 全部用 `operator.add` + 节点内逻辑 | ⚠️ 未做到（灵活但 reducer 层无业务语义） |

**如果重新设计的改进方向**：
- 为 evidence_pool 实现自定义 reducer（按 source_id 去重 + 保留最高 reliability_score），减少节点内去重代码
- 在节点输出处添加 pydantic 模型校验（如 `PlanOutput`、`AnalyzeOutput`），开发期 `validate=True`，生产期可关闭

### 自评清单

- [x] 能否说出 3 个适用场景和 2 个不适用场景？
  - 适用：多智能体并行协作、需要 checkpoint 恢复的长时间任务、HITL 人机协作
  - 不适用：简单单轮问答（State 开销过大）、纯流式管道无状态共享（如简单 ETL）
- [x] 能否说出最常用的 5 个 API/配置？
  - `StateGraph(State)`、`add_node`/`add_edge`/`add_conditional_edges`、`Annotated[T, reducer]`、`compile(checkpointer=...)`、`astream(stream_mode=[...])`
- [x] 能否描述 2 个常见故障的排查思路？
  - 字段静默丢弃（日志对照返回 dict 键与 State 定义）、并行节点互相覆盖（检查 reducer 是否指定）
- [x] 能否说清核心工作流程的关键步骤？
  - State 定义 → 初始 State 工厂 → 节点返回 dict → reducer 合并 → checkpoint 持久化
- [x] 能否结合项目回答"为什么选它？遇到什么问题？怎么解决的？"
- [x] 能否回答"如果 QPS/数据量翻 10 倍，现有方案的瓶颈和改进？"
  - 瓶颈：State dict 随迭代轮次线性增长（evidence_pool 累加），大 State 的 checkpoint 序列化开销；改进：实现 State 字段的 TTL 清理或滑动窗口，自定义 reducer 做容量上限

---

## 四、技术点：Runtime 架构（AgentBundle + Checkpointer + 图编译）

> 版本基准：LangGraph v1.0.3 · langgraph-checkpoint-postgres v3.0.5 · 核查日期 2026-09-04
> 项目场景：DeepResearch 后端，8 个 Agent 角色，PostgreSQL checkpointer，3 点 HITL

### 痛点 → 方案 → 效果

**原始痛点**：旧 `main.py`（551 行）将所有 Agent 构建逻辑、节点实现、配置加载混杂在一起，且与 `nodes.py` 的节点实现并存——同名函数静态覆盖，运行时行为不确定。Checkpointer 无降级策略，PG 不可用直接崩溃。全部节点绑死单一模型和温度，无法按节点角色差异化配置。

**核心方案**：三层 Runtime 架构。

```python
# 第一层：AgentBundle — 聚合 8 个 Agent 实例
@dataclass(frozen=True)
class AgentBundle:
    intent_router: any    # 温度 0.0，路由稳定
    planner: any          # 温度 0.3，创造性规划
    scout_web: any        # 温度 0.4，多样检索
    scout_local: any      # 温度 0.4
    evidence_judge: any  # 温度 0.2，严谨裁判
    analyst: any          # 温度 0.3
    direct_responder: any # 温度 0.2
    writer: any           # 温度 0.4，丰富写作

# 第二层：模型工厂 — 按节点配模型
def build_agents(model, api_key, config) -> AgentBundle:
    node_models = getattr(config, "node_models", None) or {}
    def _model_for(node_key, default_temp):
        node_cfg = node_models.get(node_key, {})
        node_model = node_cfg.get("model", model)
        node_temp = node_cfg.get("temperature", default_temp)
        return build_agent(node_model, api_key, node_key, node_temp, [])
    return AgentBundle(
        intent_router=_model_for("intent_router", 0.0),
        planner=_model_for("plan", 0.3),
        # ...
    )

# 第三层：Checkpointer 降级链
def build_checkpointer(config):
    # PG → Redis → InMemorySaver 三级降级
    if backend in {"postgres", "auto"} and config.enable_memory:
        # PostgresSaver.from_conn_string(dsn)
    if backend in {"redis", "auto"} and config.redis_url:
        # RedisSaver.from_conn_string(url)
    return InMemorySaver()  # 最终兜底
```

**为什么这样设计**：
1. `frozen=True` dataclass：AgentBundle 不可变，构建后不会被意外修改，保证图编译后节点绑定的 Agent 实例一致性。多个节点共用同一个 AgentBundle 实例，通过 `bind_agent` 用 `functools.partial` 将 Agent 绑定到节点函数。
2. 节点级模型配置：intent_router 温度 0.0 保证路由判定确定性（同一问题每次路由结果一致），writer 温度 0.4 保证报告写作丰富度。通过 config.json 的 `node_models` 字段，无需改代码即可为特定节点切换模型（如 plan 用 qwen-plus，direct_answer 用 qwen-turbo 降低成本）。
3. 三级降级链：生产环境 PG 可用性 > Redis > 内存。`auto` 模式自动探测——先尝试 PG，import 失败或连接失败降级 Redis，Redis 不可用降级内存。Redis 降级时还检测 RediSearch 依赖（`FT._LIST`），非 Redis Stack 自动跳过。
4. 图编译：`workflow.compile(checkpointer=checkpointer)` 将 checkpointer 注入编译后的图，`thread_id` 作为 checkpoint 的主键，实现同 thread 的状态恢复。

**效果量化对照表**：

| 指标 | 优化前（main.py 混杂） | 优化后（三层 Runtime） |
|---|---|---|
| 代码行数 | 551 行混杂 | runtime.py 157 行 + models.py 67 行 |
| 模型配置灵活度 | 全部绑死单一模型 | 节点级模型+温度配置 |
| Checkpointer 可用性 | 无降级，PG 不可用即崩 | 三级降级，自动探测 |
| Agent 实例不可变性 | 无保证 | frozen dataclass |
| 与节点解耦 | Agent 构建与节点实现混杂 | AgentBundle 通过 bind_agent 注入 |

**一句话总结**：Runtime 分三层——AgentBundle 聚合 8 个差异化 Agent，模型工厂按 config.json 节点级配置，Checkpointer 三级降级链保证可用性，frozen dataclass 保证不可变。

### 七维提问（含追问链与具体答案）

#### 1. 概念理解

**Q1: LangGraph 的 checkpointer 是什么？为什么需要它？**
A: Checkpointer 是 LangGraph 的状态持久化机制，在每个节点执行后自动将当前 State 快照写入存储。作用有三个：① 断点续研——进程崩溃后用 `astream(None, config)` 从最后 checkpoint 续跑；② HITL 恢复——interrupt 后用 `Command(resume=...)` 从中断点继续，依赖 checkpoint 保存的中间状态；③ 时间旅行——`get_state_history()` 获取历史快照，可回退到任意节点重新执行。

**Q2: `thread_id` 在 LangGraph 中的作用是什么？**
A: `thread_id` 是 checkpointer 的主键之一。`config = {"configurable": {"thread_id": thread_id}}` 传入 `astream/ainvoke`，checkpointer 用它区分不同会话的状态。同一 thread_id 的多次调用共享同一份 checkpoint 历史——这就是多轮对话能"记住"之前状态的原因。不同 thread_id 完全隔离。

#### 2. 核心原理

**Q3: `bind_agent` 用 `functools.partial` 做了什么？为什么不直接在节点函数里实例化 Agent？**
A: `bind_agent(node_func, agent, agent_name)` 返回 `partial(node_func, agent=agent, agent_name=agent_name)`。LangGraph 的 `add_node` 接收一个签名为 `(state) -> state` 的函数，但我们的节点函数签名是 `(state, agent, agent_name, writer=None)`。partial 预填充了 agent 和 agent_name 参数，使 LangGraph 调用时只需传入 state（和可选的 writer）。不在节点内实例化 Agent 的原因：① Agent 构建有开销（初始化 LLM 连接、加载 prompt），应在图编译前一次性完成；② AgentBundle 是不可变单例，图编译后所有节点共享同一组 Agent 实例，避免重复创建。

**追问链：writer 参数怎么传进来的？partial 没有预填充它。**
→ `writer` 是 LangGraph 的 `StreamWriter`，在 `compile()` 时由 LangGraph 自动注入。节点函数签名 `(state, agent, agent_name, writer=None)` 中 writer 有默认值 None，partial 不绑定它，LangGraph 在运行时检测到函数有 writer 参数会自动传入。这样节点内 `writer({"type": "token", "text": ...})` 发出的自定义事件就能通过 `astream(stream_mode=["custom"])` 消费到。

#### 3. 项目实战

**Q4: 你项目里 Checkpointer 是怎么初始化的？PG 不可用时怎么降级？**
A: `build_checkpointer(config)` 按三级降级链初始化：
1. 如果 `backend in {"postgres","auto"}` 且 `enable_memory=True` 且 `postgres_dsn` 非空 → 尝试 `import langgraph.checkpoint.postgres`，成功则 `PostgresSaver.from_conn_string(dsn).__enter__()` 并 `.setup()` 建表；
2. PG 不可用（import 失败或连接失败），如果 `backend in {"redis","auto"}` 且 `redis_url` 非空 → 尝试 `RedisSaver.from_conn_string(url)`，还检测 RediSearch（`FT._LIST`）依赖，非 Redis Stack 自动跳过；
3. 最终 `return InMemorySaver()` 兜底。

**追问链：`auto` 模式下，如果 PG 和 Redis 都不可用，用户怎么知道降级到了内存？**
→ 日志输出。每级降级都用 `logger.warning` 或 `logger.info` 打印降级原因（PG import 失败的 error message、Redis 连接失败的异常信息）。前端无感知——内存 checkpointer 功能完整，只是进程重启后状态丢失。生产环境监控应告警"checkpointer 降级到内存"事件。

**追问链：`CHECKPOINTER_CONTEXT` 全局变量是干什么的？**
→ 它是 PG/Redis checkpointer 的 context manager 引用（`from_conn_string()` 返回的是 context manager，`__enter__()` 返回实际 saver）。保存全局引用是为了在进程退出时 `__exit__()` 清理连接。但当前代码没有显式的退出清理逻辑，依赖进程退出时自动回收。

#### 4. 配置 API

**Q5: config.json 的 `node_models` 怎么配置节点级模型？**
A: config.json 中增加 `node_models` 字段，格式为 `{"节点名": {"model": "模型名", "temperature": 温度}}`。例如：
```json
{
  "node_models": {
    "plan": {"model": "qwen-plus", "temperature": 0.3},
    "direct_answer": {"model": "qwen-turbo", "temperature": 0.2}
  }
}
```
`build_agents()` 中 `_model_for(node_key, default_temp)` 读取 `node_models.get(node_key, {})`，未配置的节点用默认 model 和 `default_temp` 参数指定的温度。

**追问链：如果我想动态切换某个节点的模型（不重启服务），怎么实现？**
→ 当前不支持热更新——AgentBundle 是 frozen 的，图编译后不可变。要实现热更新需要：① 将 `build_agents` 和 `build_app` 包装为可重建函数；② 用配置版本号检测变更；③ 用新 AgentBundle 重新编译图并替换 `ResearchService._app`。但 LangGraph 编译开销不大（主要是 Agent 实例化），可以做。当前设计选择"不可变 + 重启更新"是简洁性权衡。

#### 5. 故障调优

**Q6: 你项目里遇到过 checkpointer 的问题吗？怎么排查的？**
A: 遇到过 Redis checkpointer 初始化失败的问题。Redis checkpointer 依赖 RediSearch 模块（`FT._LIST` 命令），但我们的 Redis 是标准版不是 Redis Stack，缺少 RediSearch。排查方式：在 `build_checkpointer` 中捕获异常并检查 `last_exc` 是否包含 `FT._LIST`，如果是则明确日志输出"Redis checkpointer 依赖 RediSearch(FT._LIST)。当前 Redis 非 Redis Stack，已降级"。修复方式是切到 PostgreSQL checkpointer 或升级到 Redis Stack。

**追问链：如果 PG checkpointer 建表失败（权限不足），会怎样？**
→ `checkpointer.setup()` 抛异常，被 `try/except` 捕获，`logger.warning` 打印失败原因，然后继续降级到 Redis 或内存。不会阻塞服务启动。但用户的所有 checkpoint 都在内存中，进程重启后丢失——需要监控告警。

#### 6. 对比选型

**Q7: 为什么用 LangGraph 的 checkpointer 而不是自己实现状态持久化？**
A: 自己实现需要处理三个复杂问题：① 序列化——State 中包含 LangChain 的 `BaseMessage` 对象，需要自定义序列化/反序列化；② 状态合并——每个节点返回部分更新，需要正确合并到快照；③ interrupt 恢复——需要知道中断在哪个节点的哪一步，恢复时从正确位置继续。LangGraph 的 checkpointer 内置这些能力——`PostgresSaver` 用 JSONB 存储 State，自动处理 BaseMessage 序列化，`get_state()` 返回的 `StateSnapshot` 包含 `next`（下一步节点）、`interrupts`（中断信息）、`parent_config`（父快照引用）。自己实现的投入产出比不划算。

**追问链：LangGraph checkpointer 有哪些实现可选？**
→ LangGraph 官方提供：`InMemorySaver`（开发用）、`PostgresSaver`（生产推荐，`langgraph-checkpoint-postgres`）、`RedisSaver`（需 Redis Stack/RediSearch）。社区有 MongoDB、SQLite 等实现。选择依据：PG 适合已有 PG 基础设施的项目（我们的记忆系统也用 PG），Redis 适合低延迟场景但依赖 RediSearch。

#### 7. 延伸深挖

**Q8: `graph.compile(checkpointer=checkpointer)` 编译后发生了什么？图是预编译的还是运行时构建的？**
A: `compile()` 做三件事：① 验证图拓扑——检查是否有死节点（不可达）、是否有循环但无出口（死锁）；② 构建 channel 映射——从 State 类的 `__annotations__` 提取每个字段的 reducer，为每个字段创建 channel 对象；③ 注入 checkpointer 和其他中间件（如 StreamWriter）。编译结果是 `CompiledStateGraph` 对象，不可变——后续调用 `astream/ainvoke/get_state` 都操作这个编译后的图实例。图拓扑在编译时确定，运行时不可变；但 State 内容通过 checkpointer 持久化，每个 thread_id 维护独立的状态链。

**追问链：如果运行时要动态增删节点（如根据意图跳过某些节点），怎么做？**
→ 两种方式：① 条件路由——用 `add_conditional_edges` 在路由函数中根据 State 决定下一步走哪个节点（如我们的 `route_after_intent` 和 `should_continue_research`）；② `Command(goto=...)` — 节点返回 `Command` 对象动态指定下一步节点（如 plan 节点 revise 时 `Command(goto="plan")` 回环）。这些都是编译时预定义的边，运行时只是选择走哪条边。真正动态增删节点需要重新编译图，当前项目不支持。

### 反向选型题

**Q: 为什么不用 LangChain 的 `init_chat_model` 替代手写 `build_agent`？**
A: `init_chat_model` 是 LangChain 1.x 的统一模型初始化 API，支持通过 `configurable_fields` 动态配置模型参数。不用的原因：① 我们用阿里通义千问（ChatTongyi），`init_chat_model` 对国产模型的支持不如直接 `ChatTongyi(model=...)` 直接；② `build_agent` 需要绑定 system prompt（从 `PROMPTS` 字典取），`init_chat_model` 只初始化模型不含 prompt 绑定，需要额外的 `.bind(system_prompt=...)` 调用；③ 节点级温度差异化通过 `build_agent` 的 `temperature` 参数直接传入，清晰直观。如果未来要支持多供应商（如切换到 OpenAI/DeepSeek），可以引入 `init_chat_model` 作为工厂的内部实现，但当前 ChatTongyi 满足需求。

### 行业最佳实践对照

| 业界方案 | 本项目实现 | 状态 |
|---|---|---|
| Agent 构建与节点解耦（open_deep_research 模式） | AgentBundle + bind_agent(partial) | ✅ 已做到 |
| 节点级模型配置 | config.json node_models 字段 | ✅ 已做到 |
| Checkpointer 多级降级 | PG → Redis → InMemorySaver 三级 | ✅ 已做到 |
| Agent 实例不可变 | frozen dataclass | ✅ 已做到 |
| 图编译时绑定 checkpointer | `compile(checkpointer=...)` | ✅ 已做到 |
| 模型热更新（不重启切换） | 不支持，需重启 | ⚠️ 未做到 |
| 多供应商模型切换 | 绑死 ChatTongyi | ⚠️ 未做到 |
| Checkpointer 连接池管理 | 依赖 context manager，无显式池管理 | ⚠️ 未做到 |

**如果重新设计的改进方向**：
- 引入 `init_chat_model` 作为模型工厂内部实现，通过 `base_url + api_key` 支持任意 OpenAI 兼容 API
- 实现图重建机制：配置版本号检测 + 原子替换 `_app` 引用，支持不重启切换模型
- 添加 checkpointer 健康检查探针：定期 `SELECT 1` 探活，不可用时自动降级

### 自评清单

- [x] 能否说出 3 个适用场景和 2 个不适用场景？
  - 适用：多智能体协作（差异化角色）、需断点续研的长时间任务、HITL 人机协作
  - 不适用：单一 LLM 调用（AgentBundle 开销过大）、无状态共享的管道（checkpointer 无用）
- [x] 能否说出最常用的 5 个 API/配置？
  - `build_agents(model, api_key, config)`、`build_checkpointer(config)`、`graph.compile(checkpointer=...)`、`bind_agent(func, agent, name)`、`config = {"configurable": {"thread_id": ...}}`
- [x] 能否描述 2 个常见故障的排查思路？
  - Redis checkpointer 初始化失败（检测 FT._LIST 依赖）、Agent 绑定错误（日志检查 bind_agent 的 agent_name）
- [x] 能否说清核心工作流程的关键步骤？
  - 配置加载 → build_agents → build_checkpointer → compile → ResearchService 持有编译图 → astream/ainvoke 调用
- [x] 能否结合项目回答"为什么选它？遇到什么问题？怎么解决的？"
- [x] 能否回答"如果 QPS/数据量翻 10 倍，现有方案的瓶颈和改进？"
  - 瓶颈：AgentBundle 单例无法水平扩展（多 worker 各自构建）、PG checkpointer 写入压力（每个节点一个快照）、ChatTongyi API 限流；改进：AgentBundle 无状态可多实例、checkpointer 分库分表按 thread_id hash、模型调用加限流+缓存

---

## 五、技术点：图拓扑与条件路由

> 版本基准：LangGraph v1.0.3 · 核查日期 2026-09-04
> 项目场景：10 节点 StateGraph，条件路由 + 并行边 + 回环 + HITL interrupt

### 痛点 → 方案 → 效果

**原始痛点**：旧图拓扑中 intent 节点只有 direct/research 两条路由，没有 clarify 节点，用户问题模糊时直接进入 plan 导致研究方向偏移。没有 HITL，用户无法在中途干预。

**核心方案**：

```python
def build_app(agents, checkpointer):
    workflow = StateGraph(AgentState)
    # 10 个节点
    workflow.add_node("intent", bind_agent(intent_node, agents.intent_router, "intent_router"))
    workflow.add_node("clarify", clarify_node)          # 无 Agent，纯 interrupt 逻辑
    workflow.add_node("plan", bind_agent(plan_node, agents.planner, "planner"))
    workflow.add_node("web_search", ...)
    workflow.add_node("local_rag", ...)
    workflow.add_node("deep_dive", ...)
    workflow.add_node("analyze", ...)
    workflow.add_node("reflect", ...)
    workflow.add_node("write", ...)
    workflow.add_node("direct_answer", ...)

    # 拓扑
    workflow.add_edge(START, "intent")
    workflow.add_conditional_edges("intent", route_after_intent, {...})
    workflow.add_edge("clarify", "plan")
    workflow.add_edge("plan", "web_search")    # 并行边 1
    workflow.add_edge("plan", "local_rag")    # 并行边 2
    workflow.add_edge("web_search", "deep_dive")
    workflow.add_edge("local_rag", "deep_dive")
    workflow.add_edge("deep_dive", "analyze")
    workflow.add_conditional_edges("analyze", should_continue_research, {...})
    workflow.add_edge("reflect", "web_search")  # 回环
    workflow.add_edge("reflect", "local_rag")   # 回环
    workflow.add_edge("direct_answer", END)
    workflow.add_edge("write", END)

    return workflow.compile(checkpointer=checkpointer)
```

**为什么这样设计**：
1. **并行边**：`plan → web_search` 和 `plan → local_rag` 同时添加，LangGraph 自动并行调度。web 检索和本地知识库检索独立执行，`deep_dive` 等两者都完成后才执行（两个边都指向 deep_dive，LangGraph 的 barrier 语义）。
2. **条件路由**：`route_after_intent` 根据 `state["intent"]` 返回 `"direct_answer"` 或 `"clarify"`；`should_continue_research` 根据 `iteration >= max_iterations` 和 `needs_more_research` 决定走 `reflect`（补搜回环）还是 `write`（出报告）。
3. **回环**：`reflect → web_search/local_rag` 形成循环，每次补搜迭代 `iteration += 1`，达到 `max_iterations` 强制走 write。防死循环由 `max_iterations` 兜底。
4. **HITL interrupt**：clarify、plan、write 三个节点内调用 `interrupt(kind=...)`，LangGraph 暂停执行，等待 `Command(resume=...)` 恢复。plan 节点的 `Command(goto="plan", update={...})` 实现 revise 回环，带 `plan_revision_count` 上限 3 防死循环。

**一句话总结**：图拓扑用条件路由做意图分流，并行边做双源检索，回环 + 计数器做补搜迭代上限，interrupt + Command 做 HITL 三点干预。

### 七维提问（精选）

#### 项目实战

**Q1: web_search 和 local_rag 并行执行后，deep_dive 怎么知道两者都完成了？**
A: LangGraph 的 barrier 语义——两条边都指向 `deep_dive`，LangGraph 在两个并行节点都完成后才触发 deep_dive。这是 LangGraph 的 superstep 机制：同一 superstep 内的并行节点全部完成后，才进入下一个 superstep。如果 web_search 慢于 local_rag，deep_dive 会等待 web_search 完成。

#### 故障调优

**Q2: 如果 reflect 回环导致死循环（needs_more_research 永远为 True），怎么防？**
A: 两道防线：① `should_continue_research` 检查 `iteration >= max_iterations`，达到上限强制走 write；② `reflect_node` 返回 `{"iteration": state.get("iteration", 0) + 1}`，每次回环递增。`max_iterations` 默认 3，配置可调。即使 LLM 每次都判定"需要更多研究"，最多 3 轮后强制出报告。

#### 延伸深挖

**Q3: `Command(goto="plan", update={...})` 和 `add_conditional_edges` 有什么区别？**
A: `add_conditional_edges` 是编译时预定义的路由——定义"节点 A 完成后，根据 State 决定走 B 还是 C"，路由逻辑在编译时确定。`Command(goto=...)` 是运行时动态路由——节点函数返回 `Command` 对象，指定下一步去哪个节点并携带 State 更新。区别：`Command` 可以在节点内部根据复杂逻辑决定去向（如 plan 节点根据 user decision 的 action 字段决定 approve/revise/reject 三分支），且可以携带 State 更新（如 revise 时更新 `plan_revision_count`）。`Command` 更灵活但更难静态分析。

### 反向选型题

**Q: 为什么不用 LangGraph 的 `Send` API 做 map-reduce 式并行（如对每个子问题并行检索）？**
A: `Send` API 允许一个节点向多个实例发送消息（类似 map-reduce 的 map 阶段），适合"一个子问题一个搜索节点实例"的场景。不用的原因：① 我们的 web_search 和 local_rag 是按检索类型而非子问题分拆的——web_search 一次检索所有子问题的查询，不是每个子问题一个节点实例；② `Send` 会创建多个节点实例，每个实例有独立 State 副本，合并逻辑更复杂；③ 当前设计在 web_search 节点内部循环所有子问题查询（`for query_item in queries: records = web_search_records(...)`），简单直接。如果子问题数量动态且每个需要独立深度检索，`Send` 更合适。

### 行业最佳实践对照

| 业界方案 | 本项目实现 | 状态 |
|---|---|---|
| 条件路由分流 | `add_conditional_edges` + 路由函数 | ✅ 已做到 |
| 并行节点 + barrier | 双并行边 → deep_dive | ✅ 已做到 |
| 回环 + 迭代上限 | reflect 回环 + max_iterations | ✅ 已做到 |
| HITL interrupt | 3 点 interrupt + Command | ✅ 已做到 |
| 动态 map-reduce（Send API） | 未使用，循环替代 | ⚠️ 未做到（当前子问题数固定） |
| 子图嵌套 | 未使用 | ⚠️ 未做到（10 节点规模无需） |

### 自评清单

- [x] 能否说出 3 个适用场景和 2 个不适用场景？
  - 适用：多步骤研究流程、需要人机协作审批、需要补搜迭代
  - 不适用：单轮问答（图开销过大）、线性管道无分支（条件路由无用）
- [x] 能否说出最常用的 5 个 API/配置？
  - `StateGraph(State)`、`add_node`、`add_edge`、`add_conditional_edges`、`compile(checkpointer=...)`
- [x] 能否描述 2 个常见故障的排查思路？
  - 并行节点死锁（检查 barrier 边是否正确）、回环死循环（检查 iteration 递增和 max_iterations 兜底）
- [x] 能否说清核心工作流程的关键步骤？
  - 定义节点 → 连接边 → 条件路由 → 编译 → astream/ainvoke 调用
- [x] 能否结合项目回答"为什么选它？遇到什么问题？怎么解决的？"
- [x] 能否回答"如果 QPS/数据量翻 10 倍，现有方案的瓶颈和改进？"
  - 瓶颈：单图实例无法水平扩展、并行节点数固定（无弹性）、回环迭代无法跨实例分布式执行；改进：按 thread_id 分片到不同 worker、引入 LangGraph 的分布式执行（Pregel 模式）、子图嵌套实现模块化扩展

---

## 六、技术点：流式输出与断点续研

> 版本基准：LangGraph v1.0.3 · 核查日期 2026-09-04
> 项目场景：FastAPI SSE + async generator + TaskRegistry

### 痛点 → 方案 → 效果

**原始痛点**：旧 `workflow_service.py`（921 行）用后台 Thread + asyncio.Queue 桥接——worker 线程执行 LangGraph，通过 queue 发事件到主协程消费 yield。问题：① 跨线程传事件，取消语义混乱（cancel_event 轮询，节点内 LLM 调用无法中断）；② finally 块引用 try 内局部变量，异常路径抛 NameError 且 `__done__` 不再发出 → 前端流永久挂起；③ 非 token 级流式，报告一次性整块下发。

**核心方案**：纯 async generator + `graph.astream(stream_mode=["custom","updates"])`。

```python
async def stream_research(self, query, ...) -> AsyncGenerator[str, None]:
    """纯 async generator，直接挂 StreamingResponse；无后台线程、无队列。"""
    input_state = create_initial_state(...)
    config = {"configurable": {"thread_id": thread_id}}

    yield sse(event("run.started", ...))

    async for mode, chunk in self._app.astream(
        input_state, config, stream_mode=["custom", "updates"]
    ):
        if mode == "custom":
            # 节点内 StreamWriter 发出的自定义事件
            if chunk.get("type") == "token":
                yield sse(event("message.delta", text=chunk["text"]))
            elif chunk.get("type") == "progress":
                yield sse(event("agent.status", ...))
            elif chunk.get("type") == "sources":
                yield sse(event("sources.found", ...))
        elif mode == "updates":
            # 节点状态更新
            for node_name, node_output in chunk.items():
                if "__interrupt__" in chunk:
                    yield sse(event("interrupt.raised", ...))
                    break
                yield sse(event("agent.status", node=node_name, ...))
                if node_output.get("final"):
                    final = node_output["final"]

    yield sse(event("run.completed", ...))
```

**断点续研两种模式**：

```python
async def resume_stream(self, thread_id, resume_value=None, mode="answer"):
    config = {"configurable": {"thread_id": thread_id}}
    if mode == "continue":
        input_state = None  # 从最后 checkpoint 续跑
    else:
        input_state = Command(resume=resume_value)  # 从 interrupt 点继续

    async for mode_chunk, chunk in self._app.astream(input_state, config, ...):
        ...
```

**为什么这样设计**：
1. **纯 async generator 替代 Thread+Queue**：`graph.astream()` 本身是 async generator，直接 `async for` 消费，无需跨线程桥接。CancelledError 在 generator 内部的 await 点抛出，真正中断 LLM 调用。
2. **双 stream_mode**：`"custom"` 捕获节点内 `writer({"type": "token", "text": ...})` 发出的自定义事件（token 级流式、进度、来源）；`"updates"` 捕获节点完成后的状态更新（agent.status、interrupt 检测、final 提取）。
3. **三不变式**：① 流一定结束——completed/cancelled/error 在各自分支内发出；② delta 顺序拼接完整——astream 顺序消费顺序 yield；③ 前端忽略未知 type——协议层约定。
4. **TaskRegistry**：`thread_id → asyncio.Task` 映射，同一 thread 并发 /run → `ConcurrentRunError` → 409。cancel 用 `task.cancel()` 而非标志位轮询。Redis 兜底信号支持多 worker 场景的崩溃恢复扫描。

**一句话总结**：流式用纯 async generator + 双 stream_mode 实现 token 级 SSE，TaskRegistry 管并发和取消，断点续研用 `astream(None)` 续跑或 `Command(resume=...)` 回答 interrupt。

### 七维提问（精选）

#### 核心原理

**Q1: `graph.astream(stream_mode=["custom","updates"])` 的两个 mode 分别什么时候触发？**
A: `"custom"` 在节点函数内部调用 `writer(dict)` 时立即触发——比如 write 节点内 `agent.astream(stream_mode="messages")` 的每个 token chunk 都调用 `writer({"type":"token","text":text})`，前端立即收到 message.delta。`"updates"` 在节点函数返回后触发——LangGraph 将节点返回的 dict 合并到 State 后，发出 `{node_name: node_output_dict}` 事件。所以 custom 是节点执行中的实时流，updates 是节点完成后的状态变更。

#### 项目实战

**Q2: 你的 `TaskRegistry` 怎么实现并发控制和取消？**
A: `register(thread_id, run_id, coro)` 检查 `_tasks` 字典中该 thread_id 是否有未完成的 task，有则抛 `ConcurrentRunError`（router 层转 409）。`asyncio.create_task(coro)` 创建 task 后存入注册表，`task.add_done_callback(cleanup)` 注册自动清理回调。`cancel(thread_id)` 调用 `entry.task.cancel()`，CancelledError 在 generator 的 await 点抛出，被 `except asyncio.CancelledError` 捕获后发 `run.cancelled` 事件。Redis 兜底：`register` 时 `SETEX cancel:{thread_id} 86400 "running"`，`cancel` 时如果本进程无该 task 则向 Redis 发 `SET cancel:{thread_id} "1"` 信号（多 worker 场景）。

**追问链：崩溃恢复扫描怎么实现的？**
→ `scan_orphans(graph_app)` 在进程重启后执行：扫描 Redis 中 `cancel:*` 前缀的 key，值为 `"running"` 说明重启前没清理 → 检查 PG checkpointer 是否有该 thread 的 checkpoint 且 `next` 非空 → 标记 `SETEX thread:{thread_id}:interrupted_by_restart 7d "1"`。前端 `/state` 接口返回 `interrupted_by_restart: true` 提示用户可恢复。

#### 故障调优

**Q3: 如果 astream 过程中 LangGraph 抛异常，前端流会怎样？**
A: `except Exception as e` 捕获后 `yield sse(event("run.error", code=type(e).__name__, message=str(e)))`，generator 自然关闭。前端收到 run.error 后关闭 EventSource。关键设计：没有 `finally` 块——旧代码的 finally 引用 try 内局部变量导致 NameError 挂起，新代码在各自分支（completed/cancelled/error）内发出结束事件后关闭，不依赖 finally。

#### 延伸深挖

**Q4: `astream(None, config)` 怎么实现从最后 checkpoint 续跑？**
A: `astream` 的第一个参数是 input_state。传 `None` 时，LangGraph 不写入新 State，而是从 checkpointer 读取该 thread_id 的最后快照，获取 `next`（待执行节点列表），从第一个待执行节点开始继续执行。已完成的节点不会重跑——State 中已有它们的输出（如 web_evidence、evidence_pool 等都已保留）。这就是"崩溃续研不重跑"的原理。

**追问链：如果崩溃在 web_search 节点执行中（LLM 调用了一半），续跑会重新执行 web_search 吗？**
→ 不会。LangGraph 的 checkpoint 粒度是节点级——节点开始执行前先写 checkpoint（记录 `next: [该节点]`），节点完成后写 checkpoint（记录输出和 `next: [下一节点]`）。如果崩溃在节点执行中，checkpoint 的 `next` 仍指向该节点，续跑时会重新执行该节点。但已完成的节点（如 intent、plan）不会重跑，因为它们的 checkpoint 已记录 `next: [后续节点]`。

### 反向选型题

**Q: 为什么不用 WebSocket 替代 SSE 做流式输出？**
A: SSE 更适合我们的场景：① 单向服务端推送——研究过程只需要服务端 → 客户端的 token 流，不需要客户端实时回传（HITL 回答是独立的 POST /resume 请求）；② HTTP 兼容——SSE 基于 HTTP，无需升级协议，nginx/CDN 天然支持，WebSocket 需要额外配置 `proxy_read_timeout` 和 `Upgrade` 头；③ 自动重连——浏览器 EventSource API 内置断线重连，WebSocket 需手动实现；④ 简化实现——FastAPI `StreamingResponse` 直接挂 async generator，WebSocket 需要维护连接状态和消息帧解析。WebSocket 的优势是双向通信和更低开销，但我们的场景不需要。

### 行业最佳实践对照

| 业界方案 | 本项目实现 | 状态 |
|---|---|---|
| 纯 async generator 流式 | `graph.astream` + `yield sse()` | ✅ 已做到 |
| token 级流式 | `agent.astream(stream_mode="messages")` + writer | ✅ 已做到 |
| 多 stream_mode 消费 | `["custom", "updates"]` 双模式 | ✅ 已做到 |
| 并发控制（同 thread 409） | TaskRegistry + ConcurrentRunError | ✅ 已做到 |
| 真正中断 LLM 调用 | `task.cancel()` → CancelledError | ✅ 已做到 |
| 崩溃恢复扫描 | scan_orphans + Redis 兜底 | ✅ 已做到 |
| 背压控制（前端慢时限速） | 无背压，generator 天然背压 | ⚠️ 未做到（SSE 无背压机制） |
| 多 worker 广播（WebSocket rooms） | 单 worker SSE | ⚠️ 未做到（多 worker 需 Redis pub/sub） |

**如果重新设计的改进方向**：
- 引入 Redis pub/sub 支持多 worker 场景的流式广播（当前单 worker SSE 在多实例部署时无法跨 worker 推送）
- 添加背压机制：当前端消费慢时，generator 的 yield 会阻塞（asyncio 的天然背压），但 SSE 无限制缓冲可能导致内存增长

### 自评清单

- [x] 能否说出 3 个适用场景和 2 个不适用场景？
  - 适用：长文本生成流式（报告撰写）、多步骤进度反馈、HITL 中断恢复
  - 不适用：高频双向通信（用 WebSocket）、无流式需求的一次性 API（用 POST）
- [x] 能否说出最常用的 5 个 API/配置？
  - `graph.astream(input, config, stream_mode=[...])`、`asyncio.create_task(coro)`、`task.cancel()`、`Command(resume=...)`、`astream(None, config)` 续跑
- [x] 能否描述 2 个常见故障的排查思路？
  - 前端流永久挂起（检查 generator 是否在所有分支发出结束事件）、中断不恢复（检查 interrupt kind 匹配和 Command resume payload 校验）
- [x] 能否说清核心工作流程的关键步骤？
  - create_initial_state → astream → custom/updates 事件分发 → interrupt 检测 → resume/continue → run.completed
- [x] 能否结合项目回答"为什么选它？遇到什么问题？怎么解决的？"
- [x] 能否回答"如果 QPS/数据量翻 10 倍，现有方案的瓶颈和改进？"
  - 瓶颈：单 worker SSE 无法水平扩展、长连接占用 worker、PG checkpointer 高频写入；改进：Redis pub/sub 多 worker 广播、SSE 连接池管理、checkpoint 批量写入或异步写入