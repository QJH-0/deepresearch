"""nodes 包对外导出：保持与旧 nodes.py 的 import 路径兼容。

graph.py 的 `from .nodes import ...` 仍然有效。
"""
from ._shared import bind_agent, detect_intent, raise_interrupt
from .intent import intent_node, direct_answer_node
from .plan import plan_node
from .web_search import web_search_node
from .local_rag import local_rag_node
from .deep_dive import deep_dive_node
from .analyze import analyze_node, reflect_node
from .write import write_node
from .clarify import clarify_node

__all__ = [
    "bind_agent",
    "detect_intent",
    "raise_interrupt",
    "intent_node",
    "direct_answer_node",
    "plan_node",
    "web_search_node",
    "local_rag_node",
    "deep_dive_node",
    "analyze_node",
    "reflect_node",
    "write_node",
    "clarify_node",
]
