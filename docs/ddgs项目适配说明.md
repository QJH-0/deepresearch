✅ 推荐：2026‑09 最新稳定方案

不要直接用 `DuckDuckGoSearchRun`，自己封装工具，绕开 langchain‑community 的旧包装，这是目前工程上最稳的做法。

### 安装

```
pip install -U langgraph langchain langchain‑core ddgs
```

> 包名现在是 `ddgs`，不再是 `duckduckgo‑search`。

### 完整可运行（最新版，create_react_agent）

```
import os
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition, create_react_agent
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from ddgs import DDGS

# 代理环境变量（需要代理才填）
# os.environ["DDGS_PROXY"] = "http://127.0.0.1:7890"

@tool
def web_search(query: str) -> str:
    """互联网网页搜索，获取实时、最新信息。
    Args:
        query: 搜索关键词
    """
    with DDGS() as ddgs:
        # timelimit: d天 / w周 / m月 / y年；backend="lite"更稳抗反爬
        results = list(ddgs.text(
            keywords=query,
            region="wt‑wt",
            timelimit="m",
            max_results=4,
            backend="lite"
        ))
    if not results:
        return "未找到搜索结果"
    lines = []
    for idx, item in enumerate(results):
        lines.append(f"[{idx+1}] {item['title']}\n{item['body']}\nurl:{item['href']}")
    return "\n\n".join(lines)

tools = [web_search]

llm = ChatOpenAI(
    base_url="你的endpoint",
    api_key="xxx",
    model="xxx",
    temperature=0
)

# 最新版推荐API create_react_agent，自带checkpointer、流式支持
agent = create_react_agent(
    model=llm,
    tools=tools,
    debug=False
)

if __name__ == "__main__":
    resp = agent.invoke({
        "messages": [("user", "查2026大模型Agent最新技术")]
    })
    for msg in resp["messages"]:
        msg.pretty_print()
```

## 版本说明

1. `langgraph >=0.3.x`：`create_react_agent`、`ToolNode`、`tools_condition` 接口不变，是官方主推写法。

2. 不要继续使用：

   ```
   # 容易警告/空结果，不推荐
   from langchain_community.tools import DuckDuckGoSearchRun
   ```

   原因：

   ```
   langchain‑community
   ```

    内置工具还在适配旧包名，对新 

   ```
   ddgs
   ```

    库适配不完善，生产环境经常出现搜索返回空字符串。

## 高频踩坑

1. **429 / No good result**：DDG 免费接口限流，不要并发调用；`backend="lite"` 降低被拦截概率。
2. **国产模型工具调用失败**：确保模型原生支持 function‑calling，`temperature=0`。
3. **代理**：设置环境变量 `DDGS_PROXY`，不要用 requests 全局代理。
4. **异步场景**：使用 `AsyncDDGS`，不要在同步工具里跑异步。

如果你需要，我给一份：带 `MemorySaver` 持久会话 + stream 流式输出的完整示例。

---

## DeepResearch 项目实际适配记录

### 2026-09-06 修复：duckduckgo_search 8.1.1 bing 后端返回空结果

#### 故障现象

- 6 个搜索查询全部返回 0 条记录
- 日志显示 `primp` 请求 `https://www.bing.com/search` 返回 HTTP 200，但解析结果为空
- `RuntimeWarning: This package (duckduckgo_search) has been renamed to ddgs!`

#### 根因

`duckduckgo_search` 8.1.1 的 `text()` 方法中硬编码了 `backends = ["bing"]`（第 182 行），临时禁用了 html 和 lite 后端。`_text_bing` 方法用 XPath (`//li[contains(@class, 'b_algo')]`) 解析 Bing HTML 页面，但 Bing 页面结构变更或反爬机制导致**间歇性解析失败返回空列表**。

独立测试可复现：同一查询首次调用可能成功，后续调用持续返回空。

#### 修复方案

| 变更项 | 说明 |
|--------|------|
| 安装 `ddgs` 9.16.0 | 从 PyPI 官方源安装到 `llmdev` conda 环境 |
| `requirements.txt` | `duckduckgo-search>=7.0` → `ddgs>=9.0` |
| `docker-compose.app.yml` 注释 | 同步更新包名引用 |
| 代码改动 | 无需改动，`tools.py` 的 `_ddgs()` 已优先 `from ddgs import DDGS` |

新版 `ddgs` 9.16.0 的 `text()` 返回值字段（`title`/`href`/`body`）与旧版完全兼容，`_normalize_web_record` 无需调整。

#### 验证结果

- 独立搜索测试：连续 3 次 `"rag GitHub"` 均稳定返回 5 条结果
- 项目入口测试：`web_search_records("rag是什么")` 返回 5 条中文搜索结果
- 单元测试：`test_search_provider.py` 27 passed，`test_p1.py` 10 passed

#### 项目中的 Provider 链式降级架构

```
DuckDuckGoProvider (ddgs)
  ↓ 无结果
SearXNGProvider (自建，需 SEARX_URL)
  ↓ 无结果
TavilyProvider (商用兜底，需 TAVILY_API_KEY)
  ↓ 无结果
返回空列表 + warning 日志
```

配置方式：`config.json` 的 `search_providers` 字段控制 Provider 顺序，默认 `["ddgs", "searxng"]`。

#### 踩坑补充

5. **清华镜像源无 ddgs 包**：`pip install ddgs` 默认走清华镜像源会 404，需指定官方源 `-i https://pypi.org/simple`。
6. **conda run 环境隔离**：`conda run -n llmdev pip install` 可能装到 base 环境，用 `<env>/python.exe -m pip install` 更可靠。
7. **ddgs 9.x 不再支持 backend 参数选 "auto"**：默认聚合多源搜索，比旧版 bing-only 后端更稳定。
