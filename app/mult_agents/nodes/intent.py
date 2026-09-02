"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import StreamWriter

from ..state import AgentState
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs, detect_intent
from ._parsing import _last_content, _invoke_json_agent

logger = logging.getLogger("mult_agents")


def intent_node(state: AgentState, agent, agent_name: str, writer: StreamWriter | None = None) -> AgentState:
    logger.info("%s 开始 | agent=%s", colorize("[intent]", "cyan"), colorize(agent_name, "magenta"))
    if writer:
        writer({"node": "intent", "message": "正在判断问题意图..."})
    rule_route = detect_intent(state["query"])
    prompt = (
        f"用户问题：{state['query']}\n"
        f"规则引擎初判：{rule_route}\n"
        "请输出 JSON：{\"route\":\"direct|multiagent\",\"reason\":\"...\"}"
    )
    payload, content, messages = _invoke_json_agent(
        state,
        prompt,
        agent,
        agent_name,
        "intent",
        {"route": rule_route, "reason": "rule"},
        writer=writer,
    )
    route = str(payload.get("route", rule_route)).strip().lower()
    if route not in {"direct", "multiagent"}:
        route = rule_route
    logger.info("%s 路由: %s", colorize("[intent]", "green"), route)
    if writer:
        writer({"node": "intent", "message": f"意图判定完成: {route}"})
    return {"intent": route, "draft": content, "messages": messages}



async def direct_answer_node(state: AgentState, agent, agent_name: str, writer: StreamWriter | None = None) -> AgentState:
    logger.info("%s 开始 | agent=%s", colorize("[direct_answer]", "cyan"), colorize(agent_name, "magenta"))
    if writer:
        writer({"node": "direct_answer", "message": "正在生成直接回答..."})
    prompt = f"用户问题：{state['query']}"
    human = HumanMessage(content=with_memory_context(state, prompt))
    # P2: 使用 astream 实现 token 级流式
    full_content = ""
    async for chunk in agent.astream({"messages": [human]}, stream_mode="messages"):
        if isinstance(chunk, tuple) and len(chunk) == 2:
            msg_chunk, metadata = chunk
            text = getattr(msg_chunk, "content", "")
            if text and writer:
                writer({"type": "token", "node": "direct_answer", "text": text})
                full_content += text
    if not full_content:
        # 降级：astream 未产出内容时回退到 invoke
        result = agent.invoke({"messages": [human]})
        full_content = _last_content(result).strip()
    content = full_content
    emit("direct_answer", content)
    if writer:
        writer({"node": "direct_answer", "message": "回答生成完成"})
    return {
        "intent": "direct",
        "final": content,
        "draft": content,
        "analysis_summary": content,
        "needs_more_research": False,
        "messages": [human],
    }



