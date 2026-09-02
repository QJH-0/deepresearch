"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import json
import logging
import re

from langchain_core.messages import HumanMessage
from langgraph.types import StreamWriter

from ..state import AgentState
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs

logger = logging.getLogger("mult_agents")


def _last_content(result) -> str:
    content = result["messages"][-1].content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in content)
    return str(content)



def _extract_json_block(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        return cleaned[start : end + 1]
    return cleaned



def _load_json(text: str, fallback: dict) -> dict:
    try:
        value = json.loads(_extract_json_block(text))
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    return fallback



def _invoke_json_agent(state: AgentState, prompt: str, agent, agent_name: str, node: str, fallback: dict, writer: StreamWriter | None = None) -> tuple[dict, str, list]:
    """调用 agent 并解析 JSON 结果。

    Args:
        writer: LangGraph StreamWriter，如果提供则在关键步骤推送实时事件到前端。
    """
    if writer:
        writer({"node": node, "message": f"正在调用 {agent_name} 进行推理..."})
    human = HumanMessage(content=with_memory_context(state, prompt))
    result = agent.invoke({"messages": [human]})
    tools, tool_outputs = collect_tool_calls(result["messages"])
    logger.info("%s 工具: %s", colorize(f"[{node}]", "green"), ", ".join(tools) if tools else "无")
    if writer and tools:
        writer({"node": node, "message": f"调用了工具: {', '.join(tools)}"})
    for item in tool_outputs[:5]:
        logger.info("%s 工具输出: %s", colorize(f"[{node}]", "green"), item[:400])
    logger.info("%s LLM调用: 是 | 思考: 不可见", colorize(f"[{node}]", "yellow"))
    content = _last_content(result)
    emit(node, content)
    if writer:
        preview = content.replace("\n", " ")
        if len(preview) > 200:
            preview = preview[:200] + "..."
        writer({"node": node, "message": f"推理完成: {preview}"})
    return _load_json(content, fallback), content, [human, result["messages"][-1]]



