"""对话摘要压缩服务：当消息数超过阈值时，将旧消息压缩为一条摘要消息。

触发时机：graph 执行前（stream_research / run / resume_stream 入口），
在构建 input_state 之后、astream/ainvoke 之前调用。

策略：
  - 消息数 <= threshold：不触发，原样返回
  - 消息数 > threshold：保留最近 keep_recent 条，更早的消息用 LLM 压缩为一条 SystemMessage
  - 已有旧摘要文本会被追加到新摘要中，保持上下文连续性
"""

import logging
from typing import Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

logger = logging.getLogger("backend.summary_service")

_SUMMARY_SYSTEM_PROMPT = (
    "请将以下多轮对话历史压缩为一段简洁的中文摘要，"
    "保留关键信息：用户的核心需求、已确定的研究方向、已得出的主要结论。"
    "不要遗漏重要细节，但去除冗余和重复内容。只输出摘要正文。"
)


class SummaryService:
    """对话摘要压缩服务。

    依赖外部注入 LLM 调用函数，便于测试时 mock。
    """

    def __init__(
        self,
        api_key: str,
        model: str = "qwen-turbo",
        threshold: int = 20,
        keep_recent: int = 6,
    ):
        self._api_key = api_key
        self._model = model
        self._threshold = threshold
        self._keep_recent = keep_recent

    async def summarize_if_needed(
        self,
        messages: list[BaseMessage],
        existing_summary: str = "",
    ) -> tuple[list[BaseMessage], str]:
        """判断是否需要摘要压缩，如需要则调用 LLM 生成摘要。

        Args:
            messages: 当前全量消息列表
            existing_summary: 已有的旧摘要文本（追加到新摘要中保持连续性）

        Returns:
            (压缩后的 messages 列表, 摘要文本)
            - 未触发时返回 (messages, existing_summary)
            - 触发时返回 (摘要消息 + 最近 keep_recent 条消息, 新摘要文本)
        """
        if len(messages) <= self._threshold:
            return messages, existing_summary

        to_summarize = messages[: -self._keep_recent]
        recent = messages[-self._keep_recent:]

        new_summary = await self._generate_summary(to_summarize, existing_summary)
        if not new_summary:
            logger.warning("摘要生成失败，返回原始消息列表")
            return messages, existing_summary

        summary_text = new_summary if not existing_summary else f"{existing_summary}\n\n{new_summary}"
        summary_msg = SystemMessage(content=f"[对话摘要]\n{summary_text}")

        logger.info(
            "对话摘要压缩完成 | 原始消息数=%d | 压缩后=%d | 摘要长度=%d",
            len(messages),
            len(recent) + 1,
            len(summary_text),
        )
        return [summary_msg] + recent, summary_text

    async def _generate_summary(
        self,
        messages: list[BaseMessage],
        existing_summary: str,
    ) -> str:
        """调用 LLM 生成摘要文本。"""
        from langchain_community.chat_models import ChatTongyi

        lines = []
        if existing_summary:
            lines.append(f"[之前摘要]\n{existing_summary}\n")
        lines.append("[待压缩对话]")
        for msg in messages:
            role = self._role_name(msg)
            content = self._truncate(str(getattr(msg, "content", "")), 500)
            lines.append(f"{role}: {content}")

        prompt = f"{_SUMMARY_SYSTEM_PROMPT}\n\n" + "\n".join(lines)

        try:
            llm = ChatTongyi(
                model=self._model,
                temperature=0.1,
                dashscope_api_key=self._api_key,
            )
            resp = await llm.ainvoke([HumanMessage(content=prompt)])
            return resp.content.strip()
        except Exception as exc:
            logger.warning("LLM 摘要调用失败: %s", exc)
            return ""

    @staticmethod
    def _role_name(msg: BaseMessage) -> str:
        msg_type = getattr(msg, "type", "")
        if msg_type == "human":
            return "用户"
        if msg_type == "ai":
            return "助手"
        if msg_type == "system":
            return "系统"
        return msg_type or "未知"

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        return text[:max_len] + ("..." if len(text) > max_len else "")


# ── 单例 ──────────────────────────────────────────

_SERVICE: Optional[SummaryService] = None


def get_summary_service() -> Optional[SummaryService]:
    """获取 SummaryService 单例（未初始化时返回 None）。"""
    return _SERVICE


def init_summary_service(
    api_key: str,
    model: str = "qwen-turbo",
    threshold: int = 20,
    keep_recent: int = 6,
) -> SummaryService:
    """初始化 SummaryService 单例（lifespan 启动时调用）。"""
    global _SERVICE
    _SERVICE = SummaryService(
        api_key=api_key,
        model=model,
        threshold=threshold,
        keep_recent=keep_recent,
    )
    return _SERVICE
