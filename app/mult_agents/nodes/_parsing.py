"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import json
import logging
import re

from langchain_core.messages import HumanMessage, AIMessage
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



def _extract_content_from_chunk(msg_chunk) -> str:
    """从 AIMessageChunk 提取文本内容。"""
    text = getattr(msg_chunk, "content", "")
    if isinstance(text, list):
        return "\n".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in text)
    return str(text) if text else ""



def _extract_reasoning_from_chunk(msg_chunk) -> str:
    """从 AIMessageChunk 提取 reasoning_content（深度思考增量）。"""
    reasoning = getattr(msg_chunk, "reasoning_content", None)
    if not reasoning:
        additional = getattr(msg_chunk, "additional_kwargs", None)
        if isinstance(additional, dict):
            reasoning = additional.get("reasoning_content", "")
    return str(reasoning) if reasoning else ""



async def _invoke_json_agent(state: AgentState, prompt: str, agent, agent_name: str, node: str, fallback: dict, writer: StreamWriter | None = None) -> tuple[dict, str, list]:
    """调用 agent 并解析 JSON 结果（async + astream token 级流式）。

    通过 agent.astream(stream_mode="messages") 实现 token 级增量推送，
    同时累加完整内容供 JSON 解析。astream 结束后合成完整 AIMessage 返回。
    """
    if writer:
        writer({"node": node, "message": f"正在调用 {agent_name} 进行推理..."})
    human = HumanMessage(content=with_memory_context(state, prompt))

    content = ""
    tool_calls_chunks = []

    async for chunk in agent.astream({"messages": [human]}, stream_mode="messages"):
        if isinstance(chunk, tuple) and len(chunk) == 2:
            msg_chunk, metadata = chunk
            reasoning = _extract_reasoning_from_chunk(msg_chunk)
            if reasoning and writer:
                writer({"type": "thinking", "node": node, "text": reasoning})
            text = _extract_content_from_chunk(msg_chunk)
            if text:
                content += text
                if writer:
                    writer({"type": "token", "node": node, "text": text})
            if hasattr(msg_chunk, "tool_calls") and msg_chunk.tool_calls:
                tool_calls_chunks.extend(msg_chunk.tool_calls)

    if not content:
        result = await agent.ainvoke({"messages": [human]})
        content = _last_content(result)
        if content and writer:
            writer({"type": "token", "node": node, "text": content})
        all_messages = [human, result["messages"][-1]]
    else:
        ai_msg = AIMessage(content=content)
        if tool_calls_chunks:
            ai_msg.tool_calls = tool_calls_chunks
        all_messages = [human, ai_msg]

    tools, tool_outputs = collect_tool_calls(all_messages)
    logger.info("%s 工具: %s", colorize(f"[{node}]", "green"), ", ".join(tools) if tools else "无")
    if writer and tools:
        writer({"node": node, "message": f"调用了工具: {', '.join(tools)}"})
    for item in tool_outputs[:5]:
        logger.info("%s 工具输出: %s", colorize(f"[{node}]", "green"), item[:400])
    logger.info("%s LLM调用: 是 | 思考: 已流式推送", colorize(f"[{node}]", "yellow"))
    emit(node, content)
    if writer:
        preview = content.replace("\n", " ")
        if len(preview) > 200:
            preview = preview[:200] + "..."
        writer({"node": node, "message": f"推理完成: {preview}"})
    return _load_json(content, fallback), content, all_messages
