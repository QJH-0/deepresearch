"""Phase 3/8 测试：resume 语义——崩溃续研/不重跑 + payload 校验。

覆盖用例:
    T3-1 TaskRegistry 基本功能（register/cleanup/concurrent）
    T3-3 resume mode=continue 用 None 输入
    T3-5 崩溃续研不重跑（scan_orphans 无 Redis 时跳过）
    T3-6 cancel 幂等
    T4-7 payload 校验（kind 不匹配 / revise 缺 reason）

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_resume.py -v --asyncio-mode=auto
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))

# 直接从模块文件导入，绕过 backend/service/__init__.py 的 langgraph 依赖链
_spec_tr = importlib.util.spec_from_file_location(
    "backend.service.task_registry",
    _APP_PATH / "backend" / "service" / "task_registry.py",
)
_mod_tr = importlib.util.module_from_spec(_spec_tr)
spec_tr_loaded = False
try:
    _spec_tr.loader.exec_module(_mod_tr)
    spec_tr_loaded = True
except ImportError:
    pass

if spec_tr_loaded:
    TaskRegistry = _mod_tr.TaskRegistry
    ConcurrentRunError = _mod_tr.ConcurrentRunError
    RunningTask = _mod_tr.RunningTask
    get_task_registry = _mod_tr.get_task_registry
    init_task_registry = _mod_tr.init_task_registry
else:
    TaskRegistry = None
    ConcurrentRunError = Exception

pytestmark = pytest.mark.skipif(not spec_tr_loaded, reason="task_registry 依赖加载失败")


# ──────────────────────────────────────────────
# T3-1 TaskRegistry 基本功能
# ──────────────────────────────────────────────


class TestTaskRegistryBasic:
    """TaskRegistry 注册、清理、并发拦截。"""

    @pytest.mark.asyncio
    async def test_register_and_cleanup(self):
        """注册任务后能在注册表中找到，完成后自动清理。"""
        registry = TaskRegistry(redis=None)

        async def dummy_coro():
            import asyncio
            await asyncio.sleep(0.1)

        task = await registry.register("t1", "r1", dummy_coro())
        assert registry.is_running("t1")
        entry = registry.get_running("t1")
        assert entry is not None
        assert entry.run_id == "r1"

        await task
        assert not registry.is_running("t1")

    @pytest.mark.asyncio
    async def test_concurrent_run_raises_409(self):
        """同一 thread 并发 /run → ConcurrentRunError。"""
        registry = TaskRegistry(redis=None)

        async def long_coro():
            import asyncio
            await asyncio.sleep(10)

        await registry.register("t1", "r1", long_coro())

        with pytest.raises(ConcurrentRunError) as exc_info:
            await registry.register("t1", "r2", long_coro())
        assert exc_info.value.thread_id == "t1"

        await registry.cancel("t1")


    @pytest.mark.asyncio
    async def test_cancel_idempotent(self):
        """cancel 未运行 thread → False（幂等，不报错）。"""
        registry = TaskRegistry(redis=None)
        hit = await registry.cancel("nonexistent")
        assert hit is False

    @pytest.mark.asyncio
    async def test_cancel_done_task(self):
        """已完成 task /cancel → False。"""
        registry = TaskRegistry(redis=None)

        async def quick_coro():
            pass

        task = await registry.register("t1", "r1", quick_coro())
        await task
        import asyncio
        await asyncio.sleep(0.05)

        hit = await registry.cancel("t1")
        assert hit is False


# ──────────────────────────────────────────────
# T3-3 resume 语义验证
# ──────────────────────────────────────────────


class TestResumeSemantics:
    """ResumeRequest mode 校验。"""

    def test_resume_mode_continue(self):
        """mode=continue → resume_value 为 None。"""
        from backend.schemas.research import ResumeRequest
        req = ResumeRequest(thread_id="t1", mode="continue")
        assert req.mode == "continue"
        assert req.resume_value is None

    def test_resume_mode_answer(self):
        """mode=answer → 携带 resume_value。"""
        from backend.schemas.research import ResumeRequest
        req = ResumeRequest(thread_id="t1", mode="answer", resume_value={"action": "approve"})
        assert req.mode == "answer"

    def test_resume_mode_invalid_raises(self):
        """非法 mode → ValidationError。"""
        from backend.schemas.research import ResumeRequest
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ResumeRequest(thread_id="t1", mode="invalid")


# ──────────────────────────────────────────────
# T3-4 崩溃恢复扫描（无 Redis 时跳过）
# ──────────────────────────────────────────────


class TestCrashRecovery:
    """崩溃恢复扫描测试。"""

    @pytest.mark.asyncio
    async def test_scan_orphans_no_redis_skips(self):
        """无 Redis 时 scan_orphans 跳过，返回空列表。"""
        registry = TaskRegistry(redis=None)
        orphaned = await registry.scan_orphans(graph_app=None)
        assert orphaned == []

    @pytest.mark.asyncio
    async def test_different_threads_run_independently(self):
        """不同 thread 并发运行互不干扰。"""
        import asyncio
        registry = TaskRegistry(redis=None)

        async def coro_t1():
            await asyncio.sleep(0.1)

        async def coro_t2():
            await asyncio.sleep(0.1)

        task1 = await registry.register("t1", "r1", coro_t1())
        task2 = await registry.register("t2", "r2", coro_t2())

        assert registry.is_running("t1")
        assert registry.is_running("t2")
        assert registry.get_running("t1").run_id == "r1"
        assert registry.get_running("t2").run_id == "r2"

        await task1
        await task2
        await asyncio.sleep(0.05)

        assert not registry.is_running("t1")
        assert not registry.is_running("t2")


# ──────────────────────────────────────────────
# T4-7 payload 校验
# ──────────────────────────────────────────────


class TestResumePayloadValidation:
    """resume payload 结构校验。"""

    def test_clarify_resume_payload(self):
        from backend.schemas.research import ClarifyResumePayload
        p = ClarifyResumePayload(kind="clarification", answers=["回答1", "回答2"])
        assert p.kind == "clarification"
        assert len(p.answers) == 2

    def test_plan_approval_approve(self):
        from backend.schemas.research import PlanApprovalResumePayload
        p = PlanApprovalResumePayload(kind="plan_approval", action="approve")
        assert p.action == "approve"

    def test_plan_approval_revise_requires_reason(self):
        from backend.schemas.research import PlanApprovalResumePayload
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PlanApprovalResumePayload(kind="plan_approval", action="revise")

    def test_plan_approval_revise_with_reason(self):
        from backend.schemas.research import PlanApprovalResumePayload
        p = PlanApprovalResumePayload(kind="plan_approval", action="revise", reason="需要调整")
        assert p.reason == "需要调整"

    def test_plan_approval_invalid_action(self):
        from backend.schemas.research import PlanApprovalResumePayload
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PlanApprovalResumePayload(kind="plan_approval", action="invalid")

    def test_report_review_adopt(self):
        from backend.schemas.research import ReportReviewResumePayload
        p = ReportReviewResumePayload(kind="report_review", action="adopt")
        assert p.action == "adopt"

    def test_report_review_deepen_requires_extra(self):
        from backend.schemas.research import ReportReviewResumePayload
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ReportReviewResumePayload(kind="report_review", action="deepen")

    def test_report_review_deepen_with_extra(self):
        from backend.schemas.research import ReportReviewResumePayload
        p = ReportReviewResumePayload(
            kind="report_review", action="deepen", extra_sub_questions=["方向A", "方向B"]
        )
        assert len(p.extra_sub_questions) == 2
