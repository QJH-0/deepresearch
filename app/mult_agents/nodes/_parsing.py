"""结构化节点的调用与结果提取。

结构化节点统一走 create_agent + ProviderStrategy：provider 侧用 JSON Schema
强制输出，框架校验后放进 state 的 structured_response。这里只负责流式调用、
reasoning 透出与错误转换，不做任何解析或兜底 —— provider 已保证结构，
宽容解析只会把「约束失效」掩盖成「解析器很宽容」。
"""

from langchain.agents.structured_output import StructuredOutputError as LangChainStructuredOutputError
from langchain_core.messages import HumanMessage, AIMessage
from langgraph.types import StreamWriter

from ..state import AgentState
from ._shared import emit, with_memory_context


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
    """调用结构化执行体，返回 (校验后的模型对象, 审计消息列表)。

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


def _extract_reasoning_from_chunk(msg_chunk) -> str:
    """从 AIMessageChunk 提取 reasoning_content（深度思考增量）。"""
    reasoning = getattr(msg_chunk, "reasoning_content", None)
    if not reasoning:
        additional = getattr(msg_chunk, "additional_kwargs", None)
        if isinstance(additional, dict):
            reasoning = additional.get("reasoning_content", "")
    return str(reasoning) if reasoning else ""
