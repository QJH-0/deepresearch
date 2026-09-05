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
