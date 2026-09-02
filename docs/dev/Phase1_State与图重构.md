# Phase 1 · State 与图重构 · 开发文档

> 依据：《DeepResearch_重构详细计划.md》v1.2 第四节、第五节 5.5/5.6、第六节 Phase 1
> 工期：2～3 天｜前置依赖：Phase 0 验收通过｜后续依赖本 Phase 的：P2（流式消费新 State）、P4（HITL 节点）、P5（plan 前记忆注入）
> 参考仓库：`open_deep_research`（State 分组与动态模型，commit `1b7d2e8`）、`langgraph`（reducer 语义）

---

## 1. 目标与范围

**结论先行**：本 Phase 完成后端核心资产的"结构化重写"——State 从 41 字段扁平 TypedDict 重构为分组+reducer+校验结构；1606 行 `nodes.py` 巨文件拆为 `nodes/` 包；新增动态模型工厂；web_search 从 Bocha 换成 DuckDuckGo（带 `SearchProvider` 抽象 + 三道限流防线）。**图拓扑不变**，非流式 /run 全链路跑通即验收。

**范围边界**：
- ✅ 做：state.py 重写、nodes 拆包、models.py、graph.py 接线（含 clarify 节点壳子，不启 interrupt）、DuckDuckGo 替换
- ❌ 不做：流式输出（P2）、interrupt 真正启用（P4）、记忆（P5）、前端

## 2. 现状锚点（已核实）

| 问题 | 位置 | 说明 |
|------|------|------|
| 41 字段扁平 State | `app/mult_agents/state.py:9-50`（108 行文件） | 仅 messages 有 reducer；其余字段节点写错键被静默丢弃 |
| 引用不存在字段 hypotheses | `nodes.py:622,646` | 读 `state["hypotheses"]` 恒为 KeyError/None |
| 写不存在键 analysis_summary | `nodes.py:1010` | 静默丢弃 |
| 重复定义 _fallback_analysis | `nodes.py:635` 与 `nodes.py:1362` | 两份同名实现，行为可能漂移 |
| 巨文件难维护 | `nodes.py`（1606 行，47+ 辅助函数） | 拆包是后续所有 Phase 的地基 |
| Bocha 搜索 | `tools.py`（568 行）中 web_search 相关 | 已确认删除，换 DuckDuckGo |
| 全部节点绑死 ChatTongyi | `graph.py:79-89` `build_app(agents, checkpointer)` 的 agents 集合 | 无节点级模型配置 |

**保留资产**：`prompts.py`（8 个角色 prompt 原样保留）、`rag/core.py`（父子分块+BM25+重排+查询改写）、图拓扑（`graph.py` 的节点与边关系）。

## 3. 目标 State 设计（详细字段映射）

```python
# app/mult_agents/state.py（重写）
import operator
from typing import Annotated, List, Optional
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

# ── pydantic 节点输出校验模型（开发期 validate=True，写错键立刻报错）──
from pydantic import BaseModel

class ConversationState(TypedDict):
    messages: Annotated[list, add_messages]        # 对话流（含中间结论，供回放）
    clarifications: Annotated[list, operator.add]  # D2 澄清问答记录（P4 启用，先占位）

class ResearchState(TypedDict):
    # 身份/入口
    query: str
    user_id: str
    tenant_id: str
    intent: str                                    # direct | research
    # 计划（可被 HITL 逐条修改，P4 启用 revise 分支）
    plan: Annotated[list[dict], operator.add]      # 研究子问题结构化列表
    plan_revision_count: int                       # revise 轮次计数（防死循环上限 3）
    # 证据（F4 溯源基础，reducer 累加去重在节点内用 _dedupe_sources 实现）
    sources: Annotated[list[dict], operator.add]   # 统一 Source 结构
    findings: Annotated[list[dict], operator.add]  # 每个子问题的发现
    # 报告
    report: Optional[str]
    needs_more_research: bool
    # 旧字段按语义归组保留的（audit_flags、evidence_pool 等在拆包时逐个决定去留）
    # 原则：无节点真实读写的字段直接删除，不搬运"以防万一"的字段

class ProgressState(TypedDict):
    current_node: str
    iteration: int                                 # reflect 轮次
    max_iterations: int
    started_at: float

class AgentState(ConversationState, ResearchState, ProgressState):
    """多重继承组合，替代 41 字段扁平结构"""
```

**字段迁移规则**（拆包时逐字段执行，写进 PR 描述）：

| 旧字段（state.py:9-50） | 去向 |
|---|---|
| query / user_id / tenant_id / intent | ResearchState 保留 |
| messages | ConversationState（reducer 换 add_messages） |
| plan / outline / sub_questions / research_questions | **合并收敛为 `plan: list[dict]`**（结构化子问题列表，schema 见 P4 的 plan payload）；旧扁平字符串字段删除 |
| web_search / local_rag / web_evidence / local_evidence / evidence_pool | **收敛为 `sources` + `findings`**；节点内部中间态不放 State，放节点局部变量 |
| search_plan / budget / query_traces | 评估：仅当前节点消费的 → 局部变量；跨节点消费的 → ResearchState 保留 |
| deep_dive / audit / audit_flags / analysis_summary | 收敛为 `findings` 条目的字段（每个 finding: {sub_question, evidence_ids, conclusion, confidence}） |
| iteration / max_iterations / phase | ProgressState |
| hypotheses | **删除**（本就不存在引用价值，nodes.py:622,646 的引用一并删） |
| memory_context | 保留在 ResearchState（P5 改为 langmem 注入点） |

## 4. 任务分解

### P1-1 重写 state.py

**步骤**（先测试后实现）：
1. 写契约测试 `app/test/test_state.py`：构造 `AgentState`，模拟两个节点分别返回 `{"sources": [s1]}`、`{"sources": [s2]}`，断言合并后 `[s1, s2]`（reducer 生效）；断言写入未声明键时（开发期校验开启）抛错。
2. 按第 3 节实现新 state.py。
3. 每个节点输出用 pydantic 模型校验（如 `PlanOutput`、`FindingsOutput`），开发期 `validate=True`——写错键立刻报错而不是静默丢弃（修复 nodes.py:1010 一类问题的机制保证）。

**参考**：`open_deep_research:src/open_deep_research/state.py:62-96`（AgentInputState/AgentState 分组与 `Annotated[..., override_reducer | operator.add]` 写法）。

### P1-2 拆分 nodes 包

**目标目录**：

```
app/mult_agents/nodes/
  __init__.py          # 对外导出全部节点函数（graph.py import 路径不变或统一改）
  _shared.py           # bind_agent / emit / collect_tool_calls / 日志辅助
  _parsing.py          # _extract_json_block / _load_json / _invoke_json_agent
  _evidence.py         # _filter_web_records / _filter_local_records / _dedupe_sources /
                       # _score_evidence / _assign_source_ids 等证据过滤家族
  _fallbacks.py        # _fallback_plan / _fallback_audit / _fallback_analysis（合并两份重复定义）
                       # / _render_fallback_report —— 只保留一份权威实现
  intent.py            # intent_node + direct_answer_node
  plan.py              # plan_node
  web_search.py        # web_search_node（调 SearchProvider）
  local_rag.py         # local_rag_node（调 rag/core.py）
  deep_dive.py         # deep_dive_node
  analyze.py           # analyze_node + reflect_node
  write.py             # write_node
  clarify.py           # 新增 clarify 节点壳子（P4 启用 interrupt，本 Phase 仅占位直通 plan）
```

**拆包纪律**：
1. **函数级搬迁，零行为变更**：拆包不改任何节点逻辑，只搬位置 + 改 State 键名（配合 P1-1 字段迁移）。
2. 每拆一个节点文件：跑一次对应节点契约测试（输入固定 state → 断言输出键与结构）。
3. 删除时同步清理：`nodes.py:622,646` hypotheses 引用、`nodes.py:1010` analysis_summary 写键、`codegen_node`（如 P0 未删净）。
4. 拆完后旧 `nodes.py` 删除，`graph.py` 的 `from .nodes import (...)` 改为 `from .nodes import ...`（包同名，import 路径兼容）。

**节点契约测试模板**：

```python
# app/test/test_nodes_contract.py（示例：plan 节点）
import pytest
from mult_agents.nodes.plan import plan_node, PlanOutput

def test_plan_node_output_contract(fake_agent, base_state):
    out = plan_node(base_state, fake_agent)   # fake_agent 返回固定 JSON
    PlanOutput.model_validate(out)            # 未声明键/缺键 → ValidationError
    assert {"plan"} <= set(out.keys())        # 只写 plan 与进度键
```

### P1-3 models.py 动态模型工厂

```python
# app/mult_agents/models.py
from langchain.chat_models import init_chat_model

# G5：config.json 节点级模型映射，默认 ChatTongyi(qwen)
# config.json 增配：
#   "node_models": { "plan": {"model": "qwen-plus"}, "compress": {"model": "qwen-turbo"} }
llm_template = init_chat_model(
    configurable_fields=("model", "max_tokens", "api_key", "base_url")
)
```

- `graph.py:build_app` 不再接收硬编码 agents 集合，改为按 `node_models` 配置为每个节点绑定模型实例（`.with_config(tags=["node:plan"])` 便于流式阶段按 node 归属 message）。
- 支持 OpenAI 兼容 API（DeepSeek 等）：通过 `base_url + api_key` 字段注入，不硬编码供应商。

**参考**：`open_deep_research:src/open_deep_research/deep_researcher.py:56`（configurable_model 定义）、`:81-93`（节点级绑定 with_structured_output/with_retry/with_config）。

### P1-4 graph.py 接线

- 新 State 接入保留拓扑：`START → intent →(direct|plan)→ [web_search ∥ local_rag] → deep_dive → analyze →(reflect|write)→ END`，`reflect → [web_search ∥ local_rag]` 回环不变。
- 加入 `clarify` 节点：`intent` 判定 research 后先进 `clarify`（本 Phase 直通 plan，P4 加 interrupt），边：`intent →(research)→ clarify → plan`。
- 条件路由函数 `route_after_intent` / `should_continue_research` 迁到 `nodes/_routing.py` 或保留 graph.py，改读新 State 键。

### P1-5 搜索工具替换（Bocha → DuckDuckGo）

**目标结构**：

```python
# app/mult_agents/tools.py 内新增抽象层
from typing import Protocol, runtime_checkable

@runtime_checkable
class SearchProvider(Protocol):
    async def search(self, query: str, max_results: int = 6) -> list["Source"]: ...

class DuckDuckGoProvider:
    """duckduckgo-search >=7.0；DDGS().text() 返回 title/href/body"""
    async def search(self, query: str, max_results: int = 6) -> list[Source]:
        cache_key = f"ddg:{query}"
        if cached := await self.redis.get(cache_key):
            return Source.list_validate_json(cached)        # ② Redis 缓存 TTL 1h
        try:
            raw = await asyncio.to_thread(                   # 同步库 → 不阻塞事件循环
                lambda: self._ddgs().text(query, max_results=max_results)
            )
            sources = [Source(url=r["href"], title=r["title"],
                              snippet=r["body"], source_type="web") for r in raw]
            await self.redis.setex(cache_key, 3600, Source.list_to_json(sources))
            return sources
        except Exception:                                    # ③ 失败降级：空结果不阻塞
            logger.warning("ddg search failed, degrade to empty", exc_info=True)
            return []
```

**三道限流防线**（计划 5.5 节，硬性要求）：
1. `max_results=5~8`（默认 6），单次检索量收敛；
2. Redis 结果缓存 TTL 1h（key: `ddg:{query}`）；
3. 任何异常（含 202/429 限流响应）→ 返回空列表 + 记 warning + 照常发 `sources.found`（空）事件，**绝不抛异常、绝不阻塞主流程**。

**配套改动**：
- `requirements.txt`（根目录新建，后端依赖清单首次固化）：加 `duckduckgo-search>=7.0`；删除 bocha 相关依赖。
- web_search / local_rag 工具统一返回 `Source` 结构（url、title、snippet、source_type: web|kb、chunk_id），写入 `ResearchState.sources`（节点内 `_dedupe_sources` 按 url/chunk_id 去重后返回增量）。
- 检索入口保持与 local_rag 并行（G1 拓扑不变）。
- `SearchProvider` 抽象层落位后，后续换 Tavily/Bing/SearXNG 只新增一个 Provider 实现类。

## 5. 测试计划

| 用例 | 类型 | 断言 |
|------|------|------|
| T1-1 State reducer | 单测 | sources/plan/findings/clarifications 均为累加 reducer；messages 用 add_messages |
| T1-2 节点契约 | 单测 | 每个节点（intent/plan/web_search/local_rag/deep_dive/analyze/reflect/write/clarify）输出过 pydantic 校验；写入未声明键抛错 |
| T1-3 重复实现已合并 | 静态 | `_fallback_analysis` 全局唯一定义（grep 计数=1）；hypotheses 引用零命中 |
| T1-4 模型工厂 | 单测 | config.json node_models 为空时全部节点用默认 qwen；配置 plan 节点后该节点拿到不同 model 实例 |
| T1-5 DuckDuckGo 正常路径 | 集成（可标记 skip-if-offline） | 真实查询返回 ≥5 条结果且映射为统一 Source（url 非空、source_type=web） |
| T1-6 DDG 429 注入 | 单测 | mock provider 抛限流异常 → 返回空列表、不抛异常、流程继续（节点输出合法） |
| T1-7 Redis 缓存命中 | 单测 | 同 query 二次调用不触发 DDGS().text（mock 计数=1） |
| T1-8 非流式全链路 | 集成冒烟 | POST /run（研究类问题）跑通全图并产出报告；直接问答走 direct_answer 分支 |
| T1-9 拓扑不变 | 单测 | graph.get_graph().nodes 集合 = 旧节点集 ∪ {clarify}；边关系与 graph.py:79-89 一致 |

## 6. 验收清单

- [ ] 新 `state.py` 分组结构落地，T1-1 通过
- [ ] `nodes/` 包拆分完成，旧 `nodes.py` 已删除，T1-2/T1-3 通过
- [ ] `models.py` 动态模型工厂生效，config.json 可按节点配模型（T1-4）
- [ ] graph.py 新 State 接线 + clarify 占位节点，拓扑校验通过（T1-9）
- [ ] Bocha 全部删除；DuckDuckGo 接入含三道防线，T1-5/T1-6/T1-7 通过（**429 注入测试是硬性验收项**）
- [ ] 非流式 /run 全链路跑通并产出报告（T1-8）
- [ ] pytest 全绿；打 tag `p1-done`

## 7. 风险与对策

| 风险 | 对策 |
|------|------|
| 拆包过程引入行为漂移（搬运时手滑改逻辑） | 纪律：纯搬迁与键名迁移分两个 commit；契约测试先行的每个节点都留固定输入输出的快照断言 |
| 旧 State 字段消费关系不清（某字段看似没人读，删了才炸） | 拆包前对每个字段做一次全局 grep 读写盘点，写入本文档第 3 节迁移表后再动手；不确定的先保留并标 TODO |
| DDG 在本地网络环境不可达 | T1-5 标记 skip-if-offline；不可达时以 T1-6 mock 测试为准，真网验收延后并记录到 `工程问题与解决方案记录.md` |
| `init_chat_model` 与当前 langchain 版本 API 不一致 | 以 `open_deep_research` 锁定版本的用法为准；版本对齐进 requirements.txt |
| nodes 拆包后循环 import（_shared ↔ 节点文件） | 辅助函数单向依赖：节点 → _shared/_parsing/_evidence/_fallbacks，共享层禁止 import 节点层 |
