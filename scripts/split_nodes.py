"""将 nodes.py 拆分为 nodes/ 包。

按函数行号映射，将每个文件提取到对应模块。
纪律：纯搬迁，零行为变更。
"""
import ast
import re
from pathlib import Path

NODES_FILE = Path("app/mult_agents/nodes.py")
NODES_DIR = Path("app/mult_agents/nodes")

with open(NODES_FILE, encoding="utf-8") as f:
    full_src = f.read()
    lines = full_src.splitlines(keepends=True)

tree = ast.parse(full_src)
func_map = {}
for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef,)):
        func_map[node.name] = (node.lineno, node.end_lineno)

def extract(name):
    if name not in func_map:
        return ""
    start, end = func_map[name]
    return "".join(lines[start-1:end])

def extract_range(names):
    """Extract continuous range of functions."""
    parts = []
    for n in names:
        parts.append(extract(n))
    return "\n\n\n".join(parts)

# Header (imports + module-level setup)
header = '''"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
'''

# ── _shared.py ──
shared_imports = '''import json
import logging
import os
from functools import partial

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt, StreamWriter

from ..state import AgentState

logger = logging.getLogger("mult_agents")

ANSI = {
    "reset": "\\033[0m",
    "cyan": "\\033[36m",
    "magenta": "\\033[35m",
    "yellow": "\\033[33m",
    "green": "\\033[32m",
    "red": "\\033[31m",
}
'''

with open(NODES_DIR / "_shared.py", "w", encoding="utf-8") as f:
    f.write(header)
    f.write(shared_imports + "\n\n")
    for name in ["colorize", "emit", "collect_tool_calls", "with_memory_context", "log_inputs", "detect_intent", "bind_agent"]:
        f.write(extract(name) + "\n\n\n")

# ── _parsing.py ──
parsing_imports = '''import json
import logging
import re

from langchain_core.messages import HumanMessage
from langgraph.types import StreamWriter

from ..state import AgentState
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs

logger = logging.getLogger("mult_agents")
'''

with open(NODES_DIR / "_parsing.py", "w", encoding="utf-8") as f:
    f.write(header)
    f.write(parsing_imports + "\n\n")
    for name in ["_last_content", "_extract_json_block", "_load_json", "_invoke_json_agent"]:
        f.write(extract(name) + "\n\n\n")

# ── _evidence.py ──
evidence_imports = '''import json
import logging
import re

from ..state import AgentState

logger = logging.getLogger("mult_agents")
'''

with open(NODES_DIR / "_evidence.py", "w", encoding="utf-8") as f:
    f.write(header)
    f.write(evidence_imports + "\n\n")
    for name in [
        "_default_plan", "_guess_primary_entity", "_derive_direct_search_queries",
        "_is_query_grounded", "_derive_search_plan", "_build_queries",
        "_extract_query_terms", "_estimate_relevance", "_is_bad_web_domain",
        "_filter_web_records", "_filter_local_records", "_format_raw_records",
        "_minimal_record_filter", "_assign_source_ids", "_enrich_evidence_from_raw",
        "_prune_evidence_to_allowed_sources", "_summarize_records",
        "_normalize_source_ids", "_finalize_query_traces",
        "_fallback_web_evidence", "_fallback_local_evidence",
        "_is_official_domain", "_score_evidence", "_dedupe_sources",
    ]:
        f.write(extract(name) + "\n\n\n")

# ── _fallbacks.py ──
fallbacks_imports = '''import json
import logging
import re

from ..state import AgentState

logger = logging.getLogger("mult_agents")
'''

with open(NODES_DIR / "_fallbacks.py", "w", encoding="utf-8") as f:
    f.write(header)
    f.write(fallbacks_imports + "\n\n")
    for name in [
        "_fallback_audit", "_fallback_analysis", "_render_fallback_report",
        "_build_source_lookup", "_extract_citation_ids", "_validate_and_fix_citations",
        "_render_reference_list", "_render_execution_appendix", "_ensure_reference_section",
        "_check_evidence_sufficiency",
    ]:
        f.write(extract(name) + "\n\n\n")

# ── intent.py ──
node_imports = '''import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import StreamWriter

from ..state import AgentState
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs, detect_intent
from ._parsing import _last_content, _invoke_json_agent

logger = logging.getLogger("mult_agents")
'''

with open(NODES_DIR / "intent.py", "w", encoding="utf-8") as f:
    f.write(header)
    f.write(node_imports + "\n\n")
    for name in ["intent_node", "direct_answer_node"]:
        f.write(extract(name) + "\n\n\n")

# ── plan.py ──
plan_imports = '''import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt, StreamWriter

from ..state import AgentState
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs
from ._parsing import _invoke_json_agent
from ._evidence import _default_plan, _derive_search_plan

logger = logging.getLogger("mult_agents")
'''

with open(NODES_DIR / "plan.py", "w", encoding="utf-8") as f:
    f.write(header)
    f.write(plan_imports + "\n\n")
    f.write(extract("plan_node") + "\n")

# ── web_search.py ──
ws_imports = '''import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import StreamWriter

from ..state import AgentState
from ..tools import web_search_records
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs
from ._parsing import _invoke_json_agent
from ._evidence import (
    _build_queries, _assign_source_ids, _dedupe_sources, _minimal_record_filter,
    _summarize_records, _format_raw_records, _fallback_web_evidence,
    _prune_evidence_to_allowed_sources, _enrich_evidence_from_raw,
    _finalize_query_traces,
)

logger = logging.getLogger("mult_agents")
'''

with open(NODES_DIR / "web_search.py", "w", encoding="utf-8") as f:
    f.write(header)
    f.write(ws_imports + "\n\n")
    f.write(extract("web_search_node") + "\n")

# ── local_rag.py ──
lr_imports = '''import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import StreamWriter

from ..state import AgentState
from ..tools import search_knowledge_base_records
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs
from ._parsing import _invoke_json_agent
from ._evidence import (
    _build_queries, _assign_source_ids, _dedupe_sources, _minimal_record_filter,
    _summarize_records, _format_raw_records, _fallback_local_evidence,
    _prune_evidence_to_allowed_sources, _enrich_evidence_from_raw,
    _finalize_query_traces,
)

logger = logging.getLogger("mult_agents")
'''

with open(NODES_DIR / "local_rag.py", "w", encoding="utf-8") as f:
    f.write(header)
    f.write(lr_imports + "\n\n")
    f.write(extract("local_rag_node") + "\n")

# ── deep_dive.py ──
dd_imports = '''import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt, StreamWriter

from ..state import AgentState
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs
from ._parsing import _invoke_json_agent
from ._evidence import _score_evidence, _dedupe_sources

logger = logging.getLogger("mult_agents")
'''

with open(NODES_DIR / "deep_dive.py", "w", encoding="utf-8") as f:
    f.write(header)
    f.write(dd_imports + "\n\n")
    f.write(extract("deep_dive_node") + "\n")

# ── analyze.py ──
an_imports = '''import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt, StreamWriter

from ..state import AgentState
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs
from ._parsing import _invoke_json_agent
from ._fallbacks import _fallback_analysis, _check_evidence_sufficiency

logger = logging.getLogger("mult_agents")
'''

with open(NODES_DIR / "analyze.py", "w", encoding="utf-8") as f:
    f.write(header)
    f.write(an_imports + "\n\n")
    for name in ["analyze_node", "reflect_node"]:
        f.write(extract(name) + "\n\n\n")

# ── write.py ──
wr_imports = '''import json
import logging

from langchain_core.messages import HumanMessage
from langgraph.types import interrupt, StreamWriter

from ..state import AgentState
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs
from ._parsing import _invoke_json_agent, _last_content
from ._fallbacks import (
    _render_fallback_report, _build_source_lookup, _extract_citation_ids,
    _validate_and_fix_citations, _render_reference_list,
    _render_execution_appendix, _ensure_reference_section,
    _check_evidence_sufficiency,
)

logger = logging.getLogger("mult_agents")
'''

with open(NODES_DIR / "write.py", "w", encoding="utf-8") as f:
    f.write(header)
    f.write(wr_imports + "\n\n")
    f.write(extract("write_node") + "\n")

# ── clarify.py (P4 占位) ──
clarify_src = '''"""clarify 节点（P4 HITL 启用，本 Phase 仅占位直通 plan）。"""
import logging

from ..state import AgentState

logger = logging.getLogger("mult_agents")


def clarify_node(state: AgentState) -> AgentState:
    """澄清节点占位：本 Phase 直通 plan，P4 加 interrupt。"""
    logger.info("[clarify] 占位节点直通 plan | query=%s", state.get("query", ""))
    return {"phase": "clarify_completed"}
'''

with open(NODES_DIR / "clarify.py", "w", encoding="utf-8") as f:
    f.write(clarify_src)

# ── __init__.py ──
init_src = '''"""nodes 包对外导出：保持与旧 nodes.py 的 import 路径兼容。

graph.py 的 `from .nodes import ...` 仍然有效。
"""
from ._shared import bind_agent, detect_intent
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
'''

with open(NODES_DIR / "__init__.py", "w", encoding="utf-8") as f:
    f.write(init_src)

print("nodes/ 包拆分完成！")
