"""clarify 节点（P4 HITL 启用，本 Phase 仅占位直通 plan）。"""
import logging

from ..state import AgentState

logger = logging.getLogger("mult_agents")


def clarify_node(state: AgentState) -> AgentState:
    """澄清节点占位：本 Phase 直通 plan，P4 加 interrupt。"""
    logger.info("[clarify] 占位节点直通 plan | query=%s", state.get("query", ""))
    return {"phase": "clarify_completed"}
