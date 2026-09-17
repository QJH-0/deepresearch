"""决策节点的模型输出 schema。

这些节点只产出「下一步怎么走」的判断，不含证据正文，因此适合让模型直接
产出受 schema 约束的结果，而不是解析自由文本再兜底。

字段约束的用意：把提示词里的软要求（字数上限、取值枚举）变成模型侧与校验侧
共用的硬约束 —— 提示词会被模型忽略，schema 不会。
"""

from typing import Literal

from pydantic import BaseModel, Field


class IntentDecision(BaseModel):
    """意图路由决策。"""

    route: Literal["direct", "multiagent"] = Field(description="路由目标")
    reason: str = Field(default="", max_length=200, description="判断理由")
