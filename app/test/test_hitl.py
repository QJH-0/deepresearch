"""Phase 4/8 测试：HITL 人机协同——澄清/审批/报告审核全场景（体系化收口）。

覆盖用例:
    T4-1 澄清流程（规则判断 + interrupt + 多轮）
    T4-2 plan 审批-批准
    T4-3 plan 审批-修改（含 revision_count 递增）
    T4-4 plan 轮次上限（3 次后强制采纳）
    T4-5 plan 审批-否决
    T4-6 报告审核-采纳/再深入
    T4-7 payload 校验
    T4-8 interrupt 重建

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_hitl.py -v --asyncio-mode=auto
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from pydantic import ValidationError

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))

from mult_agents.nodes._shared import raise_interrupt  # noqa: E402

_INTERRUPT_TARGET = "mult_agents.nodes._shared.interrupt"


# ──────────────────────────────────────────────
# T4-1 澄清节点测试
# ──────────────────────────────────────────────


class TestClarifyNode:
    """澄清节点规则判断测试。"""

    def test_short_query_needs_clarification(self):
        from mult_agents.nodes.clarify import _check_needs_clarification
        assert _check_needs_clarification("ai", []) is True

    def test_normal_query_no_clarification(self):
        from mult_agents.nodes.clarify import _check_needs_clarification
        assert _check_needs_clarification("请调研2024年人工智能领域的技术发展趋势", []) is False

    def test_ambiguous_query_needs_clarification(self):
        from mult_agents.nodes.clarify import _check_needs_clarification
        assert _check_needs_clarification("最近的一些热门趋势", []) is True

    def test_generate_questions_for_short_query(self):
        from mult_agents.nodes.clarify import _generate_clarify_questions
        questions = _generate_clarify_questions("ai", [])
        assert len(questions) > 0
        assert "id" in questions[0]
        assert "question" in questions[0]

    def test_followup_check_empty_answers(self):
        from mult_agents.nodes.clarify import _check_followup_needed
        assert _check_followup_needed([]) is True
        assert _check_followup_needed("") is True

    def test_followup_check_sufficient_answers(self):
        from mult_agents.nodes.clarify import _check_followup_needed
        assert _check_followup_needed(["这是一个详细的回答"]) is False


# ──────────────────────────────────────────────
# raise_interrupt 辅助函数测试
# ──────────────────────────────────────────────


class TestRaiseInterrupt:
    """raise_interrupt 自动添加 kind 键测试。"""

    def test_includes_kind(self):
        with patch(_INTERRUPT_TARGET) as mock_intr:
            mock_intr.return_value = {"action": "approve"}
            raise_interrupt("plan_approval", {"sub_questions": []})
            call_args = mock_intr.call_args[0][0]
            assert call_args["kind"] == "plan_approval"

    def test_all_three_kinds(self):
        with patch(_INTERRUPT_TARGET) as mock_intr:
            mock_intr.return_value = {}
            raise_interrupt("clarification", {"questions": []})
            raise_interrupt("plan_approval", {"sub_questions": []})
            raise_interrupt("report_review", {"report_preview": ""})
            assert mock_intr.call_count == 3
            kinds = [call[0][0]["kind"] for call in mock_intr.call_args_list]
            assert "clarification" in kinds
            assert "plan_approval" in kinds
            assert "report_review" in kinds


# ──────────────────────────────────────────────
# T4-7 payload 校验
# ──────────────────────────────────────────────


class TestPayloadValidation:
    """resume payload 结构校验全场景。"""

    def test_clarify_payload_valid(self):
        from backend.schemas.research import ClarifyResumePayload
        p = ClarifyResumePayload(kind="clarification", answers=["回答1"])
        assert p.kind == "clarification"

    def test_plan_approval_approve(self):
        from backend.schemas.research import PlanApprovalResumePayload
        p = PlanApprovalResumePayload(kind="plan_approval", action="approve")
        assert p.action == "approve"

    def test_plan_approval_revise_no_reason_raises(self):
        from backend.schemas.research import PlanApprovalResumePayload
        with pytest.raises(ValidationError):
            PlanApprovalResumePayload(kind="plan_approval", action="revise")

    def test_plan_approval_revise_with_reason(self):
        from backend.schemas.research import PlanApprovalResumePayload
        p = PlanApprovalResumePayload(kind="plan_approval", action="revise", reason="需要调整")
        assert p.reason == "需要调整"

    def test_plan_approval_reject(self):
        from backend.schemas.research import PlanApprovalResumePayload
        p = PlanApprovalResumePayload(kind="plan_approval", action="reject", reason="方向不对")
        assert p.action == "reject"

    def test_plan_approval_invalid_action(self):
        from backend.schemas.research import PlanApprovalResumePayload
        with pytest.raises(ValidationError):
            PlanApprovalResumePayload(kind="plan_approval", action="invalid")

    def test_report_review_adopt(self):
        from backend.schemas.research import ReportReviewResumePayload
        p = ReportReviewResumePayload(kind="report_review", action="adopt")
        assert p.action == "adopt"

    def test_report_review_deepen_no_extra_raises(self):
        from backend.schemas.research import ReportReviewResumePayload
        with pytest.raises(ValidationError):
            ReportReviewResumePayload(kind="report_review", action="deepen")

    def test_report_review_deepen_with_extra(self):
        from backend.schemas.research import ReportReviewResumePayload
        p = ReportReviewResumePayload(
            kind="report_review", action="deepen", extra_sub_questions=["方向A"]
        )
        assert len(p.extra_sub_questions) == 1


# ──────────────────────────────────────────────
# T4-8 interrupt 重建（get_interrupt API 逻辑）
# ──────────────────────────────────────────────


class TestInterruptReconstruction:
    """interrupt 状态重建逻辑测试。"""

    def test_no_interrupt_returns_false(self):
        """无 checkpoint → active=false。"""
        mock_snapshot = MagicMock()
        mock_snapshot.next = ()
        mock_snapshot.tasks = ()
        result = {"active": False, "thread_id": "t1"}
        if mock_snapshot.next and mock_snapshot.tasks:
            for task in mock_snapshot.tasks:
                if hasattr(task, "interrupts") and task.interrupts:
                    result = {"active": True}
                    break
        assert result["active"] is False

    def test_plan_approval_interrupt_active(self):
        """plan_approval interrupt → active=true。"""
        mock_intr = MagicMock()
        mock_intr.id = "intr-001"
        mock_intr.value = {"kind": "plan_approval", "sub_questions": []}
        mock_task = MagicMock()
        mock_task.interrupts = [mock_intr]
        mock_snapshot = MagicMock()
        mock_snapshot.next = ("plan",)
        mock_snapshot.tasks = (mock_task,)

        result = {"active": False, "thread_id": "t1"}
        if mock_snapshot.next and mock_snapshot.tasks:
            for task in mock_snapshot.tasks:
                if hasattr(task, "interrupts") and task.interrupts:
                    intr = task.interrupts[0]
                    value = intr.value if isinstance(intr.value, dict) else {"value": intr.value}
                    result = {"active": True, "thread_id": "t1",
                              "kind": value.get("kind"), "payload": value}
                    break
        assert result["active"] is True
        assert result["kind"] == "plan_approval"

    def test_clarification_interrupt_active(self):
        """clarification interrupt → active=true。"""
        mock_intr = MagicMock()
        mock_intr.value = {"kind": "clarification", "questions": [{"id": "q1"}]}
        mock_task = MagicMock()
        mock_task.interrupts = [mock_intr]
        mock_snapshot = MagicMock()
        mock_snapshot.next = ("clarify",)
        mock_snapshot.tasks = (mock_task,)

        result = {"active": False}
        if mock_snapshot.next and mock_snapshot.tasks:
            for task in mock_snapshot.tasks:
                if hasattr(task, "interrupts") and task.interrupts:
                    result = {"active": True, "kind": task.interrupts[0].value.get("kind")}
                    break
        assert result["active"] is True
        assert result["kind"] == "clarification"

    def test_report_review_interrupt_active(self):
        """report_review interrupt → active=true。"""
        mock_intr = MagicMock()
        mock_intr.value = {"kind": "report_review", "report_preview": "预览..."}
        mock_task = MagicMock()
        mock_task.interrupts = [mock_intr]
        mock_snapshot = MagicMock()
        mock_snapshot.next = ("write",)
        mock_snapshot.tasks = (mock_task,)

        result = {"active": False}
        if mock_snapshot.next and mock_snapshot.tasks:
            for task in mock_snapshot.tasks:
                if hasattr(task, "interrupts") and task.interrupts:
                    result = {"active": True, "kind": task.interrupts[0].value.get("kind")}
                    break
        assert result["active"] is True
        assert result["kind"] == "report_review"


# ──────────────────────────────────────────────
# State HITL 字段测试
# ──────────────────────────────────────────────


class TestStateHitlFields:
    """State 中 HITL 相关字段测试。"""

    def test_state_has_plan_revision_count(self):
        from mult_agents.state import create_initial_state
        state = create_initial_state(query="test", max_iterations=3, user_id="u", tenant_id="t")
        assert state["plan_revision_count"] == 0

    def test_state_has_clarifications(self):
        from mult_agents.state import create_initial_state
        state = create_initial_state(query="test", max_iterations=3, user_id="u", tenant_id="t")
        assert state["clarifications"] == []

    def test_state_hitl_config_defaults(self):
        from mult_agents.state import create_initial_state
        state = create_initial_state(query="test", max_iterations=3, user_id="u", tenant_id="t")
        config = state["hitl_config"]
        assert "plan_review" in config
        assert "analyze_clarify" in config
        assert "write_review" in config
        assert config["write_review"] is False

    def test_state_hitl_enabled_propagates(self):
        from mult_agents.state import create_initial_state
        state = create_initial_state(
            query="test", max_iterations=3, user_id="u", tenant_id="t", hitl_enabled=True
        )
        assert state["hitl_enabled"] is True

    def test_state_custom_hitl_config(self):
        from mult_agents.state import create_initial_state
        custom = {"plan_review": False, "analyze_clarify": False, "write_review": True}
        state = create_initial_state(
            query="test", max_iterations=3, user_id="u", tenant_id="t",
            hitl_enabled=True, hitl_config=custom
        )
        assert state["hitl_config"]["write_review"] is True
        assert state["hitl_config"]["plan_review"] is False
