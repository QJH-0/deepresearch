from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field, model_validator


class ResearchRequest(BaseModel):
    query: str = Field(..., min_length=1)
    user_id: str = Field(default="default_user", min_length=1)
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
    # P3: mode 区分崩溃续研 vs HITL 回答
    # mode=continue: 崩溃续研，用 astream(None, config) 从最后 checkpoint 续跑
    # mode=answer: HITL 回答，用 Command(resume=resume_value) 从 interrupt 点继续
    mode: str = Field(default="answer", pattern="^(continue|answer)$")
    resume_value: dict | str | None = None  # mode=answer 时必填


# ── P4: 结构化 resume payload（按 interrupt kind 校验） ──

class ClarifyResumePayload(BaseModel):
    """澄清回答 resume payload。"""
    kind: Literal["clarification"]
    answers: list[str]  # 与问题列表一一对应


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
    action: Literal["adopt", "deepen"]
    extra_sub_questions: list[str] = []  # deepen 必填

    @model_validator(mode="after")
    def validate_extra(self):
        if self.action == "deepen" and not self.extra_sub_questions:
            raise ValueError("deepen 操作必须提供 extra_sub_questions")
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
    """重命名会话请求。"""

    title: str = Field(..., min_length=1, max_length=120)
    user_id: str = "default_user"


class ThreadPinRequest(BaseModel):
    """置顶 / 取消置顶请求。"""

    pinned: bool
    user_id: str = "default_user"


class ThreadDeleteResponse(BaseModel):
    """删除会话响应。"""

    deleted: bool
    thread_id: str
    message: str
