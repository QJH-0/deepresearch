"""nodes 包：拆分自 nodes.py（P1-2）。

纪律：纯搬迁，零行为变更。每个文件只负责一类节点或辅助函数。
"""
import json
import logging
import re

from langchain_core.messages import HumanMessage
from langgraph.types import StreamWriter, Command

from ..state import AgentState
from ._shared import colorize, emit, with_memory_context, raise_interrupt
from ._parsing import _last_content
from ._fallbacks import (
    _render_fallback_report, _build_source_lookup, _extract_citation_ids,
    _validate_and_fix_citations, _render_reference_list,
    _render_execution_appendix, _ensure_reference_section,
    _check_evidence_sufficiency,
)

logger = logging.getLogger("mult_agents")


def _render_outline_for_prompt(outline: list) -> str:
    """把研究大纲压成提示词片段。

    只保留写作需要的字段：search_queries / status / id 是流程内部信息，
    写进提示词只会让模型分心。大纲来自 plan 节点，HITL 下是用户批准过的那一版
    —— 报告结构必须与之一致，否则用户批准的是一份、拿到的是另一份。
    """
    if not outline:
        return "（本次无预设章节，请按问题逻辑自行组织）"

    lines: list[str] = []
    for index, section in enumerate(outline, 1):
        if not isinstance(section, dict):
            continue
        title = str(section.get("title", "")).strip() or f"第 {index} 节"
        description = str(section.get("description", "")).strip()
        marks = []
        if section.get("requires_data"):
            marks.append("需数据支撑")
        if section.get("requires_chart"):
            marks.append("需图表")
        suffix = f"（{'、'.join(marks)}）" if marks else ""
        lines.append(f"{index}. {title}{suffix}：{description}" if description else f"{index}. {title}{suffix}")
    return "\n".join(lines) if lines else "（本次无预设章节，请按问题逻辑自行组织）"


async def write_node(state: AgentState, agent, agent_name: str, writer: StreamWriter = None) -> AgentState:
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
        # 流式输出提示，确保前端能收到消息内容（避免「无输出」）
        if writer:
            writer({"type": "token", "node": "write", "text": hint})
        return {"draft": hint, "final": hint, "agent_messages": []}

    valid_source_ids = [str(item.get("source_id", "")).strip() for item in state.get("source_index", []) if item.get("source_id")]
    valid_source_ids = [item for item in valid_source_ids if item][:80]
    valid_source_ids_set = set(valid_source_ids)
    
    prompt = (
        "请严格根据以下信息撰写最终的 Markdown 研报。请直接输出正文，绝对不要输出任何 JSON 结构，也不要复述你的指令。\n\n"
        f"核心问题：{state['query']}\n"
        f"子问题拆解：{json.dumps(state.get('sub_questions', []), ensure_ascii=False)}\n\n"
        "【报告章节结构（必须遵循）】：\n"
        f"{_render_outline_for_prompt(state.get('outline', []))}\n\n"
        "【分析结论 (Findings)】：\n"
        f"{json.dumps(state.get('findings', []), ensure_ascii=False)}\n\n"
        "【可用来源索引 (source_index)】：\n"
        f"{json.dumps(state.get('source_index', []), ensure_ascii=False)}\n\n"
        "【合法引用ID列表】：\n"
        f"{json.dumps(valid_source_ids, ensure_ascii=False)}\n\n"
        "【可能存在的风险/冲突 (Audit Flags)】：\n"
        f"{json.dumps(state.get('audit_flags', []), ensure_ascii=False)}\n\n"
        "要求：正文的详细分析部分必须按上述章节结构展开，每节以 `## <标题>` 开头，"
        "不得新增、合并或调换章节；标注「需数据支撑」的章节必须给出具体数据，"
        "确实查不到数据时要显式写明缺失，不要用笼统表述糊过去。\n"
        "正文必须使用合法引用ID（例如 [WEB1_1-1]、[LOC1_1-3]）；禁止使用不存在的编号。"
        "结尾不需要你来列举引用列表，系统会自动拼接。"
    )
    human = HumanMessage(content=with_memory_context(state, prompt))
    
    # 彻底断开之前的 messages 累积，只给模型当前这一条指令，避免被前面的 JSON 带偏
    if writer:
        writer({"node": "write", "message": "正在调用写作模型生成报告正文..."})
    # P2: 使用 astream 实现 token 级流式
    # P0-1：累加与 writer 推送解耦 — writer 注入失败（被 Optional 阻断）时仍保留 token 内容。
    content = ""
    async for chunk in agent.astream({"messages": [human]}, stream_mode="messages"):
        if isinstance(chunk, tuple) and len(chunk) == 2:
            msg_chunk, _ = chunk
            # R2.4: 深度思考 reasoning 增量捕获
            reasoning = getattr(msg_chunk, "reasoning_content", None)
            if not reasoning:
                additional = getattr(msg_chunk, "additional_kwargs", None)
                if isinstance(additional, dict):
                    reasoning = additional.get("reasoning_content", "")
            if reasoning and writer:
                writer({"type": "thinking", "node": "write", "text": str(reasoning)})
            text = getattr(msg_chunk, "content", "")
            if text:
                content += text
                if writer:
                    writer({"type": "token", "node": "write", "text": text})
    if not content:
        result = await agent.ainvoke({"messages": [human]})
        content = _last_content(result)
        if content and writer:
            writer({"type": "token", "node": "write", "text": content})
    
    # 强制清理可能的错误 JSON 代码块
    content = re.sub(r"^```json\s*", "", content)
    content = re.sub(r"^```markdown\s*", "", content)
    content = re.sub(r"^```\s*", "", content)
    content = re.sub(r"```$", "", content.strip())
    
    # 校验并修正引用ID，移除非法引用
    content, used_citation_ids = _validate_and_fix_citations(content, valid_source_ids_set)

    # P7-2: 引用覆盖率统计 — 主要论断数 / 带角标论断数
    sentences = re.split(r'[。.！!？?]\s*', content)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 15]
    cited = sum(1 for s in sentences if re.search(r'\[([A-Z]+\d+_\d+-\d+)\]', s))
    coverage = cited / max(len(sentences), 1)
    logger.info("[P7-2] 引用覆盖率 | 带角标论断=%d | 主要论断=%d | 覆盖率=%.1f%%",
                cited, len(sentences), coverage * 100)

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
                # “再深入方向 X” → 带方向回 plan 重新规划。
                # 方向必须走 user_feedback：plan_node 只读该通道，且会用自己生成的
                # 子问题覆盖 sub_questions，写进 sub_questions 会被静默丢弃。
                extra_sub_questions = decision.get("extra_sub_questions", [])
                iteration = state.get("iteration", 0)
                max_iter = state.get("max_iterations", 3)

                if iteration >= max_iter:
                    logger.warning("[write] 迭代已达上限 %d，直接采纳", max_iter)
                    if writer:
                        writer({"node": "write", "message": "已达迭代上限，报告自动采纳"})
                else:
                    direction = "；".join(
                        str(item).strip() for item in extra_sub_questions if str(item).strip()
                    )
                    logger.info("[write] 用户要求再深入 | 方向=%s | iteration=%d", direction, iteration + 1)
                    return Command(goto="plan", update={
                        "user_feedback": {
                            "approved": False,
                            "feedback": f"需要就以下方向继续深入：{direction}" if direction else "需要继续深入",
                        },
                        "iteration": iteration + 1,
                    })

            case "reject":
                # 否决报告 → 带理由重走规划。
                # user_feedback 必须是 dict：plan_node 按 dict 取 feedback，传裸字符串会被丢弃。
                feedback = decision.get("feedback", "")
                iteration = state.get("iteration", 0)
                max_iter = state.get("max_iterations", 3)
                if iteration >= max_iter:
                    logger.warning("[write] 迭代已达上限 %d，否决后直接采纳", max_iter)
                    if writer:
                        writer({"node": "write", "message": "已达迭代上限，报告自动采纳"})
                else:
                    logger.info("[write] 用户否决报告 | feedback=%s | iteration=%d", feedback, iteration + 1)
                    return Command(goto="plan", update={
                        "user_feedback": {"approved": False, "feedback": str(feedback)},
                        "iteration": iteration + 1,
                    })

    emit("write", final_content)
    if writer:
        writer({"node": "write", "message": "报告撰写完成"})
    return {"draft": final_content, "final": final_content, "agent_messages": [human]}

