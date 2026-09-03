"""审计修复前后端联调测试用例。

覆盖审计报告中 P0/P1/P2 级修复的契约一致性验证：
    T-A1  HITL resume payload 前后端一致性（P0-1）
    T-A2  analyze.py interrupt 使用 raise_interrupt（P0-2）
    T-A3  旧 WorkflowService 已删除（P0-3）
    T-A4  前端回滚字段适配后端契约（P0-4）
    T-A5  ReportReviewCard 动作枚举一致性（P1-4）
    T-A6  interrupt store 重建逻辑存在（P1-3）
    T-A7  weasyprint 在 requirements.txt 中（P2-1）
    T-A8  旧 sse.ts 已删除（P2-6）

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_integration_audit.py -v
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from pydantic import ValidationError

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
_FRONT_PATH = _PROJECT_ROOT / "agent_front" / "src"
sys.path.insert(0, str(_APP_PATH))


# ──────────────────────────────────────────────
# T-A1 HITL resume payload 前后端一致性（P0-1）
# ──────────────────────────────────────────────


class TestHITLPayloadConsistency:
    """前端三种卡片 emit 的 resume payload 必须能通过后端 schema 校验。"""

    def test_plan_approval_approve_payload_passes_schema(self):
        """PlanApprovalCard approve → {kind:'plan_approval', action:'approve'} 通过后端校验。"""
        from backend.schemas.research import PlanApprovalResumePayload
        # 模拟前端 emit 的 payload
        frontend_payload = {"kind": "plan_approval", "action": "approve"}
        p = PlanApprovalResumePayload(**frontend_payload)
        assert p.kind == "plan_approval"
        assert p.action == "approve"

    def test_plan_approval_revise_payload_passes_schema(self):
        """PlanApprovalCard revise → {kind:'plan_approval', action:'revise', reason:'...'} 通过后端校验。"""
        from backend.schemas.research import PlanApprovalResumePayload
        frontend_payload = {"kind": "plan_approval", "action": "revise", "reason": "需要调整方向"}
        p = PlanApprovalResumePayload(**frontend_payload)
        assert p.action == "revise"
        assert p.reason == "需要调整方向"

    def test_plan_approval_reject_payload_passes_schema(self):
        """PlanApprovalCard reject → {kind:'plan_approval', action:'reject'} 通过后端校验。"""
        from backend.schemas.research import PlanApprovalResumePayload
        frontend_payload = {"kind": "plan_approval", "action": "reject"}
        p = PlanApprovalResumePayload(**frontend_payload)
        assert p.action == "reject"

    def test_clarify_submit_payload_passes_schema(self):
        """ClarifyCard submit → {kind:'clarification', answers:[...]} 通过后端校验。"""
        from backend.schemas.research import ClarifyResumePayload
        frontend_payload = {"kind": "clarification", "answers": ["回答1", "回答2"]}
        p = ClarifyResumePayload(**frontend_payload)
        assert p.kind == "clarification"
        assert len(p.answers) == 2

    def test_clarify_skip_payload_passes_schema(self):
        """ClarifyCard skip → {kind:'clarification', answers:['']} 通过后端校验。"""
        from backend.schemas.research import ClarifyResumePayload
        frontend_payload = {"kind": "clarification", "answers": [""]}
        p = ClarifyResumePayload(**frontend_payload)
        assert p.kind == "clarification"

    def test_report_review_adopt_payload_passes_schema(self):
        """ReportReviewCard accept → {kind:'report_review', action:'adopt'} 通过后端校验。"""
        from backend.schemas.research import ReportReviewResumePayload
        frontend_payload = {"kind": "report_review", "action": "adopt"}
        p = ReportReviewResumePayload(**frontend_payload)
        assert p.action == "adopt"

    def test_report_review_deepen_payload_passes_schema(self):
        """ReportReviewCard deepen → {kind:'report_review', action:'deepen', extra_sub_questions:[...]} 通过后端校验。"""
        from backend.schemas.research import ReportReviewResumePayload
        frontend_payload = {
            "kind": "report_review",
            "action": "deepen",
            "extra_sub_questions": ["方向A", "方向B"],
        }
        p = ReportReviewResumePayload(**frontend_payload)
        assert p.action == "deepen"
        assert len(p.extra_sub_questions) == 2

    def test_payload_without_kind_fails(self):
        """缺少 kind 字段的 payload 应校验失败。"""
        from backend.schemas.research import PlanApprovalResumePayload
        with pytest.raises(ValidationError):
            PlanApprovalResumePayload(action="approve")  # 缺 kind

    def test_report_review_accept_action_not_in_schema(self):
        """旧的前端 accept 动作不在后端 schema 中，应校验失败。"""
        from backend.schemas.research import ReportReviewResumePayload
        with pytest.raises(ValidationError):
            ReportReviewResumePayload(kind="report_review", action="accept")  # accept 不在 Literal 中

    def test_report_review_reject_action_not_in_schema(self):
        """旧的前端 reject 动作不在后端 schema 中，应校验失败。"""
        from backend.schemas.research import ReportReviewResumePayload
        with pytest.raises(ValidationError):
            ReportReviewResumePayload(kind="report_review", action="reject")  # reject 不在 Literal 中


# ──────────────────────────────────────────────
# T-A2 analyze.py interrupt 使用 raise_interrupt（P0-2）
# ──────────────────────────────────────────────


class TestAnalyzeInterruptFix:
    """analyze.py 必须使用 _shared.raise_interrupt，不能直接用原始 interrupt()。"""

    def test_analyze_imports_raise_interrupt(self):
        """analyze.py 必须从 _shared 导入 raise_interrupt。"""
        src = (_APP_PATH / "mult_agents" / "nodes" / "analyze.py").read_text(encoding="utf-8")
        assert "raise_interrupt" in src, "analyze.py 应导入 raise_interrupt"
        assert "from ._shared import" in src and "raise_interrupt" in src

    def test_analyze_does_not_use_raw_interrupt(self):
        """analyze.py 不能直接调用原始 interrupt()。"""
        src = (_APP_PATH / "mult_agents" / "nodes" / "analyze.py").read_text(encoding="utf-8")
        # 不应从 langgraph.types 导入 interrupt
        assert "from langgraph.types import interrupt" not in src, \
            "不应直接从 langgraph.types 导入 interrupt"

    def test_analyze_does_not_use_type_field(self):
        """analyze.py 的 interrupt payload 不应使用 type 字段。"""
        src = (_APP_PATH / "mult_agents" / "nodes" / "analyze.py").read_text(encoding="utf-8")
        assert '"type": "analyze_clarify"' not in src, \
            '不应使用旧的 type:"analyze_clarify" 字段'

    def test_analyze_uses_clarification_kind(self):
        """analyze.py 的 interrupt 应使用 kind=clarification。"""
        src = (_APP_PATH / "mult_agents" / "nodes" / "analyze.py").read_text(encoding="utf-8")
        assert 'raise_interrupt("clarification"' in src, \
            '应调用 raise_interrupt("clarification", ...)'

    def test_raise_interrupt_adds_kind(self):
        """raise_interrupt 自动在 payload 中添加 kind 键。"""
        with patch("mult_agents.nodes._shared.interrupt") as mock_intr:
            mock_intr.return_value = {}
            from mult_agents.nodes._shared import raise_interrupt
            raise_interrupt("clarification", {"questions": ["q1"]})
            call_args = mock_intr.call_args[0][0]
            assert call_args["kind"] == "clarification"
            assert call_args["questions"] == ["q1"]


# ──────────────────────────────────────────────
# T-A3 旧 WorkflowService 已删除（P0-3）
# ──────────────────────────────────────────────


class TestWorkflowServiceRemoved:
    """旧 workflow_service.py 已删除，路由全部走 ResearchService。"""

    def test_workflow_service_file_deleted(self):
        """workflow_service.py 文件已删除。"""
        assert not (_APP_PATH / "backend" / "service" / "workflow_service.py").exists()

    def test_service_init_no_workflow_service(self):
        """backend.service.__init__ 不导出 WorkflowService。"""
        src = (_APP_PATH / "backend" / "service" / "__init__.py").read_text(encoding="utf-8")
        assert "WorkflowService" not in src
        assert "get_workflow_service" not in src

    def test_router_uses_research_service_only(self):
        """research_router.py 不引用 WorkflowService。"""
        src = (_APP_PATH / "backend" / "router" / "research_router.py").read_text(encoding="utf-8")
        assert "WorkflowService" not in src
        assert "get_workflow_service" not in src
        assert "workflow_service" not in src

    def test_research_service_has_thread_methods(self):
        """ResearchService 包含从 workflow_service 迁移的方法。"""
        src = (_APP_PATH / "backend" / "service" / "research_service.py").read_text(encoding="utf-8")
        for method in ["list_threads", "rename_thread", "set_thread_pinned",
                       "delete_thread", "get_thread_messages", "get_state_history", "update_state"]:
            assert f"def {method}" in src, f"ResearchService 应包含 {method} 方法"

    def test_research_service_no_thread_no_queue(self):
        """ResearchService 不使用 Thread 或 Queue。"""
        src = (_APP_PATH / "backend" / "service" / "research_service.py").read_text(encoding="utf-8")
        assert "threading.Thread" not in src
        assert "asyncio.Queue" not in src
        assert "Thread(" not in src


# ──────────────────────────────────────────────
# T-A4 前端回滚字段适配后端契约（P0-4）
# ──────────────────────────────────────────────


class TestRollbackContractFix:
    """前端 fetchHistory/rollbackThread 适配后端字段。"""

    def test_fetch_history_returns_history_not_checkpoints(self):
        """fetchHistory 返回 {history:[...]} 而非 {checkpoints:[...]}。"""
        src = (_FRONT_PATH / "api" / "rest.ts").read_text(encoding="utf-8")
        assert "history" in src
        assert "CheckpointItem" in src
        # 不应再有旧的 checkpoints 返回类型
        assert "checkpoints:" not in src.split("export function fetchHistory")[1].split("}")[0]

    def test_rollback_thread_sends_values_not_checkpoint_id(self):
        """rollbackThread 发 {thread_id, values:{checkpoint_id}} 而非 {thread_id, checkpoint_id}。"""
        src = (_FRONT_PATH / "api" / "rest.ts").read_text(encoding="utf-8")
        # 查找 rollbackThread 函数的 body 部分（从函数名到闭合大括号）
        rollback_start = src.find("export function rollbackThread")
        assert rollback_start != -1
        # 取整个函数体到下一个空行
        rollback_end = src.find("\n}", rollback_start)
        rollback_section = src[rollback_start:rollback_end]
        assert "values" in rollback_section
        assert "checkpoint_id" in rollback_section

    def test_rollback_menu_uses_history_field(self):
        """RollbackMenu.vue 使用 result.history 而非 result.checkpoints。"""
        src = (_FRONT_PATH / "components" / "chat" / "RollbackMenu.vue").read_text(encoding="utf-8")
        assert "result.history" in src
        assert "result.checkpoints" not in src

    def test_rollback_menu_uses_checkpoint_id_field(self):
        """RollbackMenu.vue 使用 cp.checkpoint_id 而非 cp.id。"""
        src = (_FRONT_PATH / "components" / "chat" / "RollbackMenu.vue").read_text(encoding="utf-8")
        assert "cp.checkpoint_id" in src
        assert "cp.id" not in src.replace("cp.checkpoint_id", "")

    def test_rollback_menu_uses_created_at_field(self):
        """RollbackMenu.vue 使用 cp.created_at 而非 cp.ts。"""
        src = (_FRONT_PATH / "components" / "chat" / "RollbackMenu.vue").read_text(encoding="utf-8")
        assert "cp.created_at" in src

    def test_backend_rollback_request_schema(self):
        """后端 RollbackRequest 仍要求 {thread_id, values, as_node?}。"""
        from backend.schemas.research import RollbackRequest
        r = RollbackRequest(thread_id="t1", values={"checkpoint_id": "cp1"})
        assert r.thread_id == "t1"
        assert r.values == {"checkpoint_id": "cp1"}
        assert r.as_node is None


# ──────────────────────────────────────────────
# T-A5 ReportReviewCard 动作枚举一致性（P1-4）
# ──────────────────────────────────────────────


class TestReportReviewCardActionFix:
    """ReportReviewCard 动作枚举与后端 schema 一致。"""

    def test_report_review_card_uses_adopt(self):
        """ReportReviewCard 使用 'adopt' 而非 'accept'。"""
        src = (_FRONT_PATH / "components" / "chat" / "ReportReviewCard.vue").read_text(encoding="utf-8")
        assert "'adopt'" in src or '"adopt"' in src
        assert "'accept'" not in src.replace("'adopt'", "").replace('"adopt"', "")

    def test_report_review_card_no_reject(self):
        """ReportReviewCard 不包含 reject 动作调用（注释说明除外）。"""
        src = (_FRONT_PATH / "components" / "chat" / "ReportReviewCard.vue").read_text(encoding="utf-8")
        # 检查不存在 emit('resume', { action: 'reject' }) 调用
        assert "action: 'reject'" not in src
        assert 'action: "reject"' not in src

    def test_report_review_card_has_kind(self):
        """ReportReviewCard 的 emit 包含 kind:'report_review'。"""
        src = (_FRONT_PATH / "components" / "chat" / "ReportReviewCard.vue").read_text(encoding="utf-8")
        assert "kind" in src
        assert "report_review" in src


# ──────────────────────────────────────────────
# T-A6 interrupt store 重建逻辑存在（P1-3）
# ──────────────────────────────────────────────


class TestInterruptStoreRebuild:
    """interrupt store 包含 rebuild 方法。"""

    def test_interrupt_store_has_rebuild(self):
        """interrupt.ts 包含 rebuild 函数。"""
        src = (_FRONT_PATH / "stores" / "interrupt.ts").read_text(encoding="utf-8")
        assert "rebuild" in src
        assert "async function rebuild" in src

    def test_interrupt_store_calls_backend_api(self):
        """rebuild 方法调用 GET /threads/{id}/interrupt。"""
        src = (_FRONT_PATH / "stores" / "interrupt.ts").read_text(encoding="utf-8")
        assert "/interrupt" in src
        assert "fetch" in src

    def test_interrupt_store_has_kind_check(self):
        """rebuild 方法检查返回的 kind 字段。"""
        src = (_FRONT_PATH / "stores" / "interrupt.ts").read_text(encoding="utf-8")
        assert "kind" in src


# ──────────────────────────────────────────────
# T-A7 weasyprint 在 requirements.txt 中（P2-1）
# ──────────────────────────────────────────────


class TestWeasyprintDependency:
    """requirements.txt 包含 weasyprint。"""

    def test_weasyprint_in_requirements(self):
        """requirements.txt 包含 weasyprint。"""
        req_path = _PROJECT_ROOT / "requirements.txt"
        content = req_path.read_text(encoding="utf-8")
        assert "weasyprint" in content.lower(), "requirements.txt 应包含 weasyprint"


# ──────────────────────────────────────────────
# T-A8 旧 sse.ts 已删除（P2-6）
# ──────────────────────────────────────────────


class TestOldSseDeleted:
    """旧 utils/sse.ts 已删除。"""

    def test_old_sse_ts_deleted(self):
        """utils/sse.ts 文件已删除。"""
        assert not (_FRONT_PATH / "utils" / "sse.ts").exists()

    def test_no_imports_of_old_sse(self):
        """没有文件引用旧的 utils/sse。"""
        # 搜索所有 .ts/.vue 文件中是否有 from.*utils/sse 的导入
        import os
        for root, dirs, files in os.walk(str(_FRONT_PATH)):
            for fname in files:
                if fname.endswith((".ts", ".vue")):
                    fpath = Path(root) / fname
                    content = fpath.read_text(encoding="utf-8", errors="ignore")
                    assert "utils/sse" not in content, \
                        f"{fpath} 仍引用旧的 utils/sse"


# ──────────────────────────────────────────────
# T-A9 _validate_resume_payload 路由级校验（P0-1 回归）
# ──────────────────────────────────────────────


class TestRouterPayloadValidation:
    """路由层 _validate_resume_payload 按 kind 校验 payload。"""

    def test_validate_plan_approval_passes(self):
        """plan_approval kind + 正确 payload → 不抛异常。"""
        from backend.router.research_router import _validate_resume_payload
        _validate_resume_payload("plan_approval", {
            "kind": "plan_approval", "action": "approve"
        })

    def test_validate_clarification_passes(self):
        """clarification kind + 正确 payload → 不抛异常。"""
        from backend.router.research_router import _validate_resume_payload
        _validate_resume_payload("clarification", {
            "kind": "clarification", "answers": ["ans1"]
        })

    def test_validate_report_review_passes(self):
        """report_review kind + 正确 payload → 不抛异常。"""
        from backend.router.research_router import _validate_resume_payload
        _validate_resume_payload("report_review", {
            "kind": "report_review", "action": "adopt"
        })

    def test_validate_unknown_kind_raises(self):
        """未知 kind → ValueError。"""
        from backend.router.research_router import _validate_resume_payload
        with pytest.raises(ValueError):
            _validate_resume_payload("unknown", {"kind": "unknown"})

    def test_validate_non_dict_raises(self):
        """非 dict payload → ValueError。"""
        from backend.router.research_router import _validate_resume_payload
        with pytest.raises(ValueError):
            _validate_resume_payload("plan_approval", "not a dict")

    def test_validate_plan_approval_revise_no_reason_raises(self):
        """plan_approval revise 无 reason → ValueError。"""
        from backend.router.research_router import _validate_resume_payload
        with pytest.raises(ValueError):
            _validate_resume_payload("plan_approval", {
                "kind": "plan_approval", "action": "revise"
            })

    def test_validate_report_review_deepen_no_extra_raises(self):
        """report_review deepen 无 extra_sub_questions → ValueError。"""
        from backend.router.research_router import _validate_resume_payload
        with pytest.raises(ValueError):
            _validate_resume_payload("report_review", {
                "kind": "report_review", "action": "deepen"
            })


# ──────────────────────────────────────────────
# T-A10 前端三卡片 emit 均含 kind 字段（P0-1 源码检查）
# ──────────────────────────────────────────────


class TestFrontendCardsHaveKind:
    """前端三卡片 emit 的 resume payload 均包含 kind 字段。"""

    def test_plan_approval_card_has_kind(self):
        """PlanApprovalCard.vue 所有 emit('resume', ...) 包含 kind:'plan_approval'。"""
        src = (_FRONT_PATH / "components" / "chat" / "PlanApprovalCard.vue").read_text(encoding="utf-8")
        assert "kind: 'plan_approval'" in src or 'kind: "plan_approval"' in src
        # 所有 emit('resume', ...) 调用都应有 kind
        lines_with_emit = [l.strip() for l in src.split('\n') if "emit('resume'" in l or 'emit("resume"' in l]
        for line in lines_with_emit:
            assert "kind" in line, f"emit 行缺少 kind: {line}"

    def test_clarify_card_has_kind(self):
        """ClarifyCard.vue 所有 emit('resume', ...) 包含 kind:'clarification'。"""
        src = (_FRONT_PATH / "components" / "chat" / "ClarifyCard.vue").read_text(encoding="utf-8")
        assert "kind: 'clarification'" in src or 'kind: "clarification"' in src
        lines_with_emit = [l.strip() for l in src.split('\n') if "emit('resume'" in l or 'emit("resume"' in l]
        for line in lines_with_emit:
            assert "kind" in line, f"emit 行缺少 kind: {line}"

    def test_report_review_card_has_kind(self):
        """ReportReviewCard.vue 所有 emit('resume', ...) 包含 kind:'report_review'。"""
        src = (_FRONT_PATH / "components" / "chat" / "ReportReviewCard.vue").read_text(encoding="utf-8")
        assert "kind: 'report_review'" in src or 'kind: "report_review"' in src
        lines_with_emit = [l.strip() for l in src.split('\n') if "emit('resume'" in l or 'emit("resume"' in l]
        for line in lines_with_emit:
            assert "kind" in line, f"emit 行缺少 kind: {line}"
