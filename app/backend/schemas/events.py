"""SSE 事件协议定义（单一事实源）。

协议不变式：
1. 流一定会结束：completed / cancelled / error 三者必有其一；
   finally 中只做"确保结束事件已发 + 关闭 generator"，不引用任何 try 块内变量。
2. message.delta 按到达顺序追加即得完整文本。
3. 前端对未知 type 静默忽略（向前兼容）。

事件清单：
    run.started       — 一次研究开始
    agent.status      — 节点级进度
    message.start     — 一条消息开始
    message.delta     — token 级增量（核心）
    message.thinking  — 思考过程增量
    sources.found     — 新来源
    interrupt.raised   — HITL 中断请求
    run.completed     — 结束，报告 message_id
    run.cancelled      — 用户取消完成
    run.error         — 任何异常必发，随后关闭流
"""

import time
from typing import Literal, Union

from pydantic import BaseModel, Field


class EventEnvelope(BaseModel):
    """SSE 行统一外层：data: {json}"""

    type: str
    ts: int = Field(default_factory=lambda: int(time.time() * 1000))
    data: dict


class SourceItem(BaseModel):
    url: str | None = None
    title: str = ""
    snippet: str = ""
    source_type: Literal["web", "kb"] = "web"
    chunk_id: str | None = None


class RunStartedData(BaseModel):
    thread_id: str
    run_id: str


class AgentStatusData(BaseModel):
    node: str
    label: str
    phase: str


class MessageStartData(BaseModel):
    message_id: str
    role: str = "assistant"
    node: str = ""


class MessageDeltaData(BaseModel):
    message_id: str
    text: str


class MessageThinkingData(BaseModel):
    message_id: str
    text: str


class SourcesFoundData(BaseModel):
    sources: list[SourceItem]


class InterruptRaisedData(BaseModel):
    interrupt_id: str
    kind: Literal["plan_approval", "clarification", "report_review"]
    payload: dict


class RunCompletedData(BaseModel):
    message_id: str
    final_state: str


class RunCancelledData(BaseModel):
    reason: str


class RunErrorData(BaseModel):
    code: str
    message: str


EVENT_REGISTRY: dict[str, type[BaseModel]] = {
    "run.started": RunStartedData,
    "agent.status": AgentStatusData,
    "message.start": MessageStartData,
    "message.delta": MessageDeltaData,
    "message.thinking": MessageThinkingData,
    "sources.found": SourcesFoundData,
    "interrupt.raised": InterruptRaisedData,
    "run.completed": RunCompletedData,
    "run.cancelled": RunCancelledData,
    "run.error": RunErrorData,
}


def event(type_: str, **data) -> EventEnvelope:
    """构造 SSE 事件信封。

    Args:
        type_: 事件类型（见 EVENT_REGISTRY）
        **data: 该事件类型的 data 字段值

    Returns:
        EventEnvelope，可通过 model_dump_json() 序列化为 SSE data 行。
    """
    model = EVENT_REGISTRY[type_](**data)
    return EventEnvelope(type=type_, data=model.model_dump())


def sse(envelope: EventEnvelope) -> str:
    """格式化为 SSE 行：data: {json}\\n\\n"""
    return f"data: {envelope.model_dump_json()}\n\n"
