"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import json
import logging
import re

from langchain.agents.structured_output import StructuredOutputError as LangChainStructuredOutputError
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.types import StreamWriter

from ..state import AgentState
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs

logger = logging.getLogger("mult_agents")


class StructuredOutputError(RuntimeError):
    """模型未能产出符合 schema 的结构化结果。"""


async def _invoke_structured_agent(
    state: AgentState,
    prompt: str,
    agent,
    agent_name: str,
    node: str,
    writer: StreamWriter | None = None,
):
    """调用决策节点的结构化执行体，返回 (校验后的模型对象, 审计消息列表)。

    输出由 provider 用 JSON Schema（strict）约束，框架按 schema 校验后放进
    state 的 structured_response，因此这里不做任何解析或兜底。

    失败一律抛 StructuredOutputError —— 不返回兜底值。是否降级由调用方决定，
    因为「能不能降级」是各节点自己的业务判断。
    """
    if writer:
        writer({"node": node, "message": f"正在调用 {agent_name} 进行推理..."})

    human = HumanMessage(content=with_memory_context(state, prompt))
    last_values = None
    try:
        async for mode, chunk in agent.runnable.astream(
            {"messages": [human]}, stream_mode=["messages", "values"]
        ):
            if mode == "values":
                last_values = chunk
                continue
            msg_chunk = chunk[0] if isinstance(chunk, tuple) else chunk
            reasoning = _extract_reasoning_from_chunk(msg_chunk)
            if reasoning and writer:
                writer({"type": "thinking", "node": node, "text": reasoning})
    except LangChainStructuredOutputError as exc:
        # 只转换「结构化语义失败」；网络、超时等异常原样上抛，不与之混为一谈
        raise StructuredOutputError(f"{agent_name} 输出不符合 schema: {exc}") from exc

    result = (last_values or {}).get("structured_response")
    if result is None:
        raise StructuredOutputError(f"{agent_name} 未返回结构化结果")

    content = result.model_dump_json()
    emit(node, content)
    if writer:
        writer({"node": node, "message": f"推理完成: {result}"})
    return result, [human, AIMessage(content=content)]


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
    """调用 agent 并解析 JSON 结果（async + astream 流式）。

    通过 agent.astream(stream_mode="messages") 增量接收并累加完整内容供 JSON 解析，
    astream 结束后合成完整 AIMessage 返回。

    **只推 reasoning 与进度，不把正文推成 message.delta**：本函数的产出是
    JSON 结构化结果，其 token 流是 JSON 的拼装过程，属于实现细节；
    推给前端会让用户看到 `{"route":"direct","reason":...}` 这类原始 JSON
    混在回答气泡里。用户可见的正文只来自 write（报告）与 direct_answer（直答）。
    """
    if writer:
        writer({"node": node, "message": f"正在调用 {agent_name} 进行推理..."})
    human = HumanMessage(content=with_memory_context(state, prompt))

    content = ""
    tool_calls_chunks = []

    async for chunk in agent.astream({"messages": [human]}, stream_mode="messages"):
        if isinstance(chunk, tuple) and len(chunk) == 2:
            msg_chunk, _ = chunk
            reasoning = _extract_reasoning_from_chunk(msg_chunk)
            if reasoning and writer:
                writer({"type": "thinking", "node": node, "text": reasoning})
            text = _extract_content_from_chunk(msg_chunk)
            if text:
                content += text
            if hasattr(msg_chunk, "tool_calls") and msg_chunk.tool_calls:
                tool_calls_chunks.extend(msg_chunk.tool_calls)

    if not content:
        # 流式无内容不再补一次 ainvoke：同一请求会重复计费，且把「模型没产出」
        # 掩盖成「换条路径再试一次」。是否降级由调用方判断，这里只保证留痕。
        logger.warning("%s 流式未返回内容，跳过二次调用 | agent=%s",
                       colorize(f"[{node}]", "yellow"), agent_name)
        if writer:
            writer({"node": node, "message": f"{agent_name} 未返回内容"})
        all_messages = [human]
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
