"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import json
import logging
import re

from langchain_core.messages import HumanMessage
from langgraph.types import StreamWriter, Command

from ..state import AgentState
from ._shared import colorize, emit, collect_tool_calls, with_memory_context, log_inputs, raise_interrupt
from ._parsing import _invoke_json_agent, _last_content
from ._fallbacks import (
    _render_fallback_report, _build_source_lookup, _extract_citation_ids,
    _validate_and_fix_citations, _render_reference_list,
    _render_execution_appendix, _ensure_reference_section,
    _check_evidence_sufficiency,
)

logger = logging.getLogger("mult_agents")


async def write_node(state: AgentState, agent, agent_name: str, writer: StreamWriter | None = None) -> AgentState:
    logger.info("%s 开始 | agent=%s", colorize("[write]", "cyan"), colorize(agent_name, "magenta"))
    if writer:
        writer({"node": "write", "message": "正在撰写最终报告..."})

    # 🔧 修复 #1：证据充分性检查 — web 召回 0 条 + 仅 1 条无关本地文档时，停止硬写报告
    is_sufficient, insufficient_reason = _check_evidence_sufficiency(state)
    if not is_sufficient:
        logger.warning("[write] 证据不足，返回明确提示 | reason=%s", insufficient_reason)
        web_stats = state.get("web_retrieval_stats", {})
        local_stats = state.get("local_retrieval_stats", {})
        hint = (
            f"# 研究无法完成：证据严重不足\n\n"
            f"**原因**：{insufficient_reason}\n\n"
            f"**检索统计**：\n"
            f"- 网页检索：{web_stats.get('query_count', 0)} 次查询，"
            f"命中 {web_stats.get('raw_count', 0)} 条，保留 {web_stats.get('kept_count', 0)} 条\n"
            f"- 本地检索：{local_stats.get('query_count', 0)} 次查询，"
            f"命中 {local_stats.get('raw_count', 0)} 条，保留 {local_stats.get('kept_count', 0)} 条\n\n"
            f"**建议**：\n"
            f"1. 尝试换个更具体的搜索词\n"
            f"2. 上传与您问题相关的本地文档后再试\n"
            f"3. 检查网络连接或配置 SearXNG/博查搜索 API Key"
        )
        return {"draft": hint, "final": hint, "messages": []}

    valid_source_ids = [str(item.get("source_id", "")).strip() for item in state.get("source_index", []) if item.get("source_id")]
    valid_source_ids = [item for item in valid_source_ids if item][:80]
    valid_source_ids_set = set(valid_source_ids)
    
    prompt = (
        "请严格根据以下信息撰写最终的 Markdown 研报。请直接输出正文，绝对不要输出任何 JSON 结构，也不要复述你的指令。\n\n"
        f"核心问题：{state['query']}\n"
        f"子问题拆解：{json.dumps(state.get('sub_questions', []), ensure_ascii=False)}\n\n"
        "【分析结论 (Findings)】：\n"
        f"{json.dumps(state.get('findings', []), ensure_ascii=False)}\n\n"
        "【可用来源索引 (source_index)】：\n"
        f"{json.dumps(state.get('source_index', []), ensure_ascii=False)}\n\n"
        "【合法引用ID列表】：\n"
        f"{json.dumps(valid_source_ids, ensure_ascii=False)}\n\n"
        "【可能存在的风险/冲突 (Audit Flags)】：\n"
        f"{json.dumps(state.get('audit_flags', []), ensure_ascii=False)}\n\n"
        "要求：正文必须使用合法引用ID（例如 [WEB1_1-1]、[LOC1_1-3]）；禁止使用不存在的编号。"
        "结尾不需要你来列举引用列表，系统会自动拼接。"
    )
    human = HumanMessage(content=with_memory_context(state, prompt))
    
    # 彻底断开之前的 messages 累积，只给模型当前这一条指令，避免被前面的 JSON 带偏
    if writer:
        writer({"node": "write", "message": "正在调用写作模型生成报告正文..."})
    # P2: 使用 astream 实现 token 级流式
    content = ""
    async for chunk in agent.astream({"messages": [human]}, stream_mode="messages"):
        if isinstance(chunk, tuple) and len(chunk) == 2:
            msg_chunk, metadata = chunk
            text = getattr(msg_chunk, "content", "")
            if text and writer:
                writer({"type": "token", "node": "write", "text": text})
                content += text
    if not content:
        # 降级：astream 未产出内容时回退到 invoke
        result = agent.invoke({"messages": [human]})
        content = _last_content(result)
    
    # 强制清理可能的错误 JSON 代码块
    content = re.sub(r"^```json\s*", "", content)
    content = re.sub(r"^```markdown\s*", "", content)
    content = re.sub(r"^```\s*", "", content)
    content = re.sub(r"```$", "", content.strip())
    
    # 校验并修正引用ID，移除非法引用
    content, used_citation_ids = _validate_and_fix_citations(content, valid_source_ids_set)
    
    final_content = _ensure_reference_section(content, state)

    # ── HITL: report_review（P4 改造：kind=report_review，支持 adopt/deepen） ──
    if state.get("hitl_enabled", False) and state.get("hitl_config", {}).get("write_review", False):
        decision = raise_interrupt("report_review", {
            "report_preview": final_content[:2000],
            "full_report": final_content,
            "message": "报告初稿已生成，请审核。",
        })

        action = decision.get("action", "adopt") if isinstance(decision, dict) else "adopt"

        match action:
            case "adopt":
                logger.info("[write] 用户采纳报告")
                pass  # 使用当前 final_content

            case "deepen":
                # “再深入方向 X” → 回检索追加子问题
                extra_sub_questions = decision.get("extra_sub_questions", [])
                iteration = state.get("iteration", 0)
                max_iter = state.get("max_iterations", 3)

                if iteration >= max_iter:
                    logger.warning("[write] 迭代已达上限 %d，直接采纳", max_iter)
                    if writer:
                        writer({"node": "write", "message": "已达迭代上限，报告自动采纳"})
                else:
                    logger.info("[write] 用户要求再深入 | 方向=%s | iteration=%d", extra_sub_questions, iteration + 1)
                    return Command(goto="plan", update={
                        "sub_questions": extra_sub_questions,
                        "iteration": iteration + 1,
                    })

    emit("write", final_content)
    if writer:
        writer({"node": "write", "message": "报告撰写完成"})
    return {"draft": final_content, "final": final_content, "messages": [human]}

