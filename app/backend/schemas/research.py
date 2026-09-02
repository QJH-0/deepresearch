from pydantic import BaseModel, Field


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
    resume_value: dict | str = Field(...)


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
