from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field, model_validator


class ResearchRequest(BaseModel):
    """研究请求。

    user_id 不在请求体里 —— 身份一律由 JWT 的 sub 派生，
    否则调用方可以自由指定他人身份（越权访问）。
    """

    query: str = Field(..., min_length=1)
    thread_id: str = Field(default="default_thread", min_length=1)
    tenant_id: str = Field(default="default_tenant", min_length=1)
    max_iterations: int | None = Field(default=None, ge=1, le=6)
    enable_memory: bool | None = None
    hitl_enabled: bool | None = None


class ResearchResponse(BaseModel):
    query: str
    user_id: str
    thread_id: str
    tenant_id: str
    final: str


class ResumeRequest(BaseModel):
    thread_id: str = Field(..., min_length=1)
    # mode=continue: 崩溃续研，用 astream(None, config) 从最后 checkpoint 续跑
    # mode=answer: HITL 回答，用 Command(resume=resume_value) 从 interrupt 点继续
    # mode=modify: 用户补充/修改条件，先 aupdate_state 追加 HumanMessage，再 astream(None, config)
    mode: str = Field(default="answer", pattern="^(continue|answer|modify)$")
    resume_value: dict | str | None = None  # mode=answer 时必填，mode=modify 时为用户消息文本


# ── P4: 结构化 resume payload（按 interrupt kind 校验） ──

class ClarifyResumePayload(BaseModel):
    """澄清回答 resume payload（对应 clarify 节点的 interrupt）。"""
    kind: Literal["clarification"]
    answers: list[str]  # 与问题列表一一对应


class EvidenceGapResumePayload(BaseModel):
    """证据缺口处置 resume payload（对应 analyze 节点的 interrupt）。

    与 clarification 是两个独立协议：clarify 收集「问题答案」，
    这里收集「缺口处置动作」。共用同一个 kind 会导致载荷结构互相冲突，
    因此用独立 kind 区分。
    """
    kind: Literal["evidence_gap"]
    action: Literal["auto_search", "user_supply", "skip"]
    info: str = ""  # user_supply 时为用户补充的信息

    @model_validator(mode="after")
    def validate_info(self):
        if self.action == "user_supply" and not self.info.strip():
            raise ValueError("user_supply 操作必须提供 info")
        return self


class PlanApprovalResumePayload(BaseModel):
    """计划审批 resume payload。"""
    kind: Literal["plan_approval"]
    action: Literal["approve", "revise", "reject"]
    reason: str | None = None  # revise 必填（model_validator 校验）

    @model_validator(mode="after")
    def validate_reason(self):
        if self.action == "revise" and not self.reason:
            raise ValueError("revise 操作必须提供 reason")
        return self


class ReportReviewResumePayload(BaseModel):
    """报告审核 resume payload。"""
    kind: Literal["report_review"]
    action: Literal["adopt", "deepen", "reject"]
    extra_sub_questions: list[str] = []  # deepen 必填
    feedback: str = ""  # reject 时的否决理由

    @model_validator(mode="after")
    def validate_extra(self):
        if self.action == "deepen" and not self.extra_sub_questions:
            raise ValueError("deepen 操作必须提供 extra_sub_questions")
        if self.action == "reject" and not self.feedback:
            raise ValueError("reject 操作必须提供 feedback")
        return self


class RollbackRequest(BaseModel):
    thread_id: str = Field(..., min_length=1)
    values: dict
    as_node: str | None = None


class InterruptInfo(BaseModel):
    interrupt_id: str
    node: str = ""
    value: dict | str
    thread_id: str
    resumable: bool = True


class TaskStatus(BaseModel):
    thread_id: str
    status: str  # running | interrupted | completed | error
    current_node: str | None = None
    query: str = ""
    created_at: str | None = None
    interrupts: list[dict] = []


# ── 会话历史（侧边栏）相关模型 ──

class ThreadItem(BaseModel):
    """会话列表项。"""

    thread_id: str
    # 降级路径（扫 checkpoints 表）拿不到 title，所以给默认值，
    # 否则 Pydantic 校验直接抛错、整个列表接口 500。
    title: str = ""             # 自动/手动命名后的展示标题
    query: str = ""             # 兼容旧字段：首条提问
    intent: str = ""
    message_count: int = 0
    completed: bool = False
    pinned: bool = False
    created_at: str = ""
    updated_at: str = ""


class ThreadListResponse(BaseModel):
    """会话列表响应。"""

    threads: list[ThreadItem]
    total: int


class ThreadRenameRequest(BaseModel):
    """重命名会话请求（user_id 由令牌派生）。"""

    title: str = Field(..., min_length=1, max_length=120)


class ThreadPinRequest(BaseModel):
    """置顶 / 取消置顶请求（user_id 由令牌派生）。"""

    pinned: bool


class ThreadDeleteResponse(BaseModel):
    """删除会话响应。"""

    deleted: bool
    thread_id: str
    message: str
