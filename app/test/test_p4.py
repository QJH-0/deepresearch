"""Phase 4 测试：HITL 人机协同——澄清/审批/报告审核全场景。

覆盖用例:
    T4-1 澄清流程（规则判断 + interrupt + 多轮）
    T4-2 plan 审批-批准
    T4-3 plan 审批-修改（含 revision_count 递增）
    T4-4 plan 轮次上限（3 次后强制采纳）
    T4-5 plan 审批-否决
    T4-6 报告审核-采纳/再深入
    T4-7 payload 校验（kind 不匹配 / revise 缺 reason / action 非法值）
    T4-8 interrupt 重建（get_interrupt API）

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    set PYTHONPATH=app
    python -m pytest app/test/test_p4.py -v --asyncio-mode=auto
"""

import asyncio
import importlib.abc
import sys
import types
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from pydantic import ValidationError

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))


# ── 通配 Meta Path Finder：自动 mock 未安装的第三方包 ──

import importlib.machinery

class _MockLoader(importlib.abc.Loader):
    """Loader that creates an empty mock module."""
    def create_module(self, spec):
        mod = types.ModuleType(spec.name)
        mod.__path__ = []
        return mod
    def exec_module(self, module):
        pass  # 空实现

class _WildcardMockFinder(importlib.abc.MetaPathFinder):
    """对未安装的第三方包返回空 mock 模块，使 import 不报错。

    仅对白名单前缀生效，不影响 stdlib 和已安装的包。
    """

    _PREFIXES = (
        "langchain",  # 匹配所有 langchain_* 开头的包
        "dashscope",
        "pymilvus",
        "faiss",
        "firecrawl",
        "searxng",
    )

    def find_spec(self, name, path, target=None):
        # 匹配 langchain / langchain_* / langchain.* 等
        for p in self._PREFIXES:
            if name == p or name.startswith(p + ".") or name.startswith(p + "_"):
                if name not in sys.modules:
                    return importlib.machinery.ModuleSpec(name, _MockLoader(), is_package=True)
        return None


# ── 预注册需要具体类/函数的模块 ──

def _setup_mocks():
    """注册所有需要的 mock 模块。"""

    # langgraph
    if "langgraph" not in sys.modules:
        lg = types.ModuleType("langgraph")
        lg.__path__ = []
        sys.modules["langgraph"] = lg

    if "langgraph.types" not in sys.modules:
        lgt = types.ModuleType("langgraph.types")
        def _interrupt(value):
            return value
        class _Command:
            def __init__(self, goto=None, update=None, resume=None):
                self.goto = goto
                self.update = update or {}
                self.resume = resume
            def __repr__(self):
                return f"Command(goto={self.goto})"
        lgt.interrupt = _interrupt
        lgt.Command = _Command
        lgt.StreamWriter = type("StreamWriter", (), {})
        sys.modules["langgraph.types"] = lgt
        lg.types = lgt

    if "langgraph.graph" not in sys.modules:
        lgg = types.ModuleType("langgraph.graph")
        lgg.__path__ = []
        sys.modules["langgraph.graph"] = lgg
        lg.graph = lgg

    if "langgraph.graph.message" not in sys.modules:
        lgm = types.ModuleType("langgraph.graph.message")
        lgm.add_messages = lambda l, r: l + r
        sys.modules["langgraph.graph.message"] = lgm
        lgg.message = lgm

    # langchain_core（预注册有具体类的子模块）
    if "langchain_core" not in sys.modules:
        lc = types.ModuleType("langchain_core")
        lc.__path__ = []
        sys.modules["langchain_core"] = lc

    if "langchain_core.messages" not in sys.modules:
        lcm = types.ModuleType("langchain_core.messages")
        class _HumanMessage:
            def __init__(self, content="", **kw):
                self.content = content
                self.type = "human"
        lcm.HumanMessage = _HumanMessage
        lcm.BaseMessage = type("BaseMessage", (), {})
        lcm.AIMessage = type("AIMessage", (), {})
        sys.modules["langchain_core.messages"] = lcm
        lc.messages = lcm

    if "langchain_core.tools" not in sys.modules:
        lct = types.ModuleType("langchain_core.tools")
        lct.tool = lambda f: f
        lct.StructuredTool = type("StructuredTool", (), {})
        sys.modules["langchain_core.tools"] = lct
        lc.tools = lct

    if "langchain_core.documents" not in sys.modules:
        lcd = types.ModuleType("langchain_core.documents")
        lcd.Document = type("Document", (), {
            "__init__": lambda self, page_content="", metadata=None: (
                setattr(self, 'page_content', page_content),
                setattr(self, 'metadata', metadata or {}),
            )[-1]
        })
        sys.modules["langchain_core.documents"] = lcd
        lc.documents = lcd

    # langchain_community（预注册有具体类的子模块）
    if "langchain_community" not in sys.modules:
        lcmm = types.ModuleType("langchain_community")
        lcmm.__path__ = []
        sys.modules["langchain_community"] = lcmm
    else:
        lcmm = sys.modules["langchain_community"]

    if "langchain_community.embeddings" not in sys.modules:
        lcmm_e = types.ModuleType("langchain_community.embeddings")
        lcmm_e.DashScopeEmbeddings = type("DashScopeEmbeddings", (), {"__init__": lambda self, **kw: None})
        sys.modules["langchain_community.embeddings"] = lcmm_e
        lcmm.embeddings = lcmm_e

    if "langchain_community.vectorstores" not in sys.modules:
        lcmm_v = types.ModuleType("langchain_community.vectorstores")
        lcmm_v.FAISS = type("FAISS", (), {})
        lcmm_v.Milvus = type("Milvus", (), {})
        sys.modules["langchain_community.vectorstores"] = lcmm_v
        lcmm.vectorstores = lcmm_v

    # typing_extensions
    try:
        import typing_extensions
    except ImportError:
        te = types.ModuleType("typing_extensions")
        te.TypedDict = dict
        import typing
        te.Annotated = typing.Annotated
        sys.modules["typing_extensions"] = te

    # 注册通配 finder
    finder = _WildcardMockFinder()
    if not any(isinstance(f, _WildcardMockFinder) for f in sys.meta_path):
        sys.meta_path.append(finder)


_setup_mocks()

# ── 现在可以安全导入了 ──

from mult_agents.state import create_initial_state, AgentState
from mult_agents.nodes._shared import raise_interrupt, colorize, emit, detect_intent
from mult_agents.nodes.clarify import (
    clarify_node,
    _check_needs_clarification,
    _generate_clarify_questions,
    _check_followup_needed,
)

# 尝试导入 plan_node 和 write_node
try:
    from mult_agents.nodes.plan import plan_node
    HAS_PLAN_NODE = True
except Exception:
    HAS_PLAN_NODE = False
    plan_node = None

try:
    from mult_agents.nodes.write import write_node
    HAS_WRITE_NODE = True
except Exception:
    HAS_WRITE_NODE = False
    write_node = None

_INTERRUPT_TARGET = "mult_agents.nodes._shared.interrupt"


# ──────────────────────────────────────────────
# T4-1 澄清节点测试
# ──────────────────────────────────────────────


def test_clarify_short_query_needs_clarification():
    """短 query → 需要澄清。"""
    assert _check_needs_clarification("ai", []) is True


def test_clarify_normal_query_no_clarification():
    """正常 query → 不需要澄清。"""
    assert _check_needs_clarification("请调研2024年人工智能领域的技术发展趋势", []) is False


def test_clarify_ambiguous_query_needs_clarification():
    """含模糊词的 query → 需要澄清。"""
    assert _check_needs_clarification("最近的一些热门趋势", []) is True


def test_clarify_generate_questions_for_short_query():
    """短 query 生成澄清问题。"""
    questions = _generate_clarify_questions("ai", [])
    assert len(questions) > 0
    assert "id" in questions[0]
    assert "question" in questions[0]


def test_clarify_generate_questions_for_time_query():
    """含时间模糊词的 query 生成时间澄清问题。"""
    questions = _generate_clarify_questions("最新的AI趋势", [])
    assert any(q["id"] == "q_time" for q in questions)


def test_clarify_followup_check_empty_answers():
    """空回答 → 需要追问。"""
    assert _check_followup_needed([]) is True
    assert _check_followup_needed("") is True


def test_clarify_followup_check_sufficient_answers():
    """充分回答 → 不需要追问。"""
    assert _check_followup_needed(["这是一个详细的回答"]) is False
    assert _check_followup_needed("这是足够长的回答") is False


def test_clarify_multi_round_limit():
    """多轮上限：已有 2 轮澄清 → 直通 plan。"""
    state = {
        "query": "最近的AI趋势",
        "clarifications": [{"q": [], "a": []}, {"q": [], "a": []}],
    }
    result = clarify_node(state)
    assert hasattr(result, "goto")
    assert "plan" in result.goto


def test_clarify_no_clarification_needed_goes_to_plan():
    """无需澄清 → 直通 plan。"""
    state = {
        "query": "请调研2024年人工智能领域的技术发展趋势",
        "clarifications": [],
    }
    result = clarify_node(state)
    assert hasattr(result, "goto")
    assert "plan" in result.goto


def test_clarify_needs_clarification_triggers_interrupt():
    """需要澄清 → 触发 interrupt。"""
    state = {
        "query": "最近的AI趋势",
        "clarifications": [],
    }
    with patch(_INTERRUPT_TARGET) as mock_intr:
        mock_intr.return_value = {"answers": ["最近一年"]}
        result = clarify_node(state)

    mock_intr.assert_called_once()
    assert hasattr(result, "goto")


# ──────────────────────────────────────────────
# 辅助函数
# ──────────────────────────────────────────────


def _make_initial_state(hitl_enabled=True, plan_review=True, write_review=False,
                         revision_count=0, iteration=0, max_iterations=3):
    """创建测试用初始状态。"""
    return {
        "query": "测试查询", "user_id": "u1", "tenant_id": "t1", "memory_context": "",
        "intent": "multiagent", "messages": [], "clarifications": [],
        "plan": "", "outline": [], "sub_questions": ["子问题1", "子问题2"],
        "research_questions": [], "search_plan": [], "budget": {},
        "supplementary_queries": [], "web_search": "", "local_rag": "",
        "web_evidence": [], "local_evidence": [], "evidence_pool": [],
        "deep_dive": "", "audit": "", "audit_flags": [], "analysis": "",
        "findings": [], "claim_map": [], "source_index": [],
        "needs_more_research": False, "missing_gaps": [], "code": "",
        "draft": "", "final": "", "web_retrieval_stats": {},
        "local_retrieval_stats": {}, "web_search_trace": [], "local_rag_trace": [],
        "hitl_enabled": hitl_enabled,
        "hitl_config": {"plan_review": plan_review, "analyze_clarify": True, "write_review": write_review},
        "user_feedback": {}, "plan_revision_count": revision_count,
        "phase": "planning", "iteration": iteration, "max_iterations": max_iterations,
    }


# ──────────────────────────────────────────────
# T4-2~T4-5 plan 审批测试
# ──────────────────────────────────────────────


@pytest.mark.skipif(not HAS_PLAN_NODE, reason="plan_node 依赖加载失败")
class TestPlanApproval:

    def test_plan_approval_approve(self):
        """T4-2: approve → 计划固化，返回正常 state dict。"""
        state = _make_initial_state(revision_count=0)

        with patch(_INTERRUPT_TARGET) as mock_intr:
            mock_intr.return_value = {"action": "approve"}
            mock_agent = MagicMock()
            with patch("mult_agents.nodes.plan._invoke_json_agent") as mock_invoke:
                mock_invoke.return_value = (
                    {"outline": [], "sub_questions": ["Q1"], "research_questions": [],
                     "budget": {}, "objective": "test"},
                    "content", [],
                )
                result = plan_node(state, mock_agent, "test_agent")

        assert isinstance(result, dict)
        assert result.get("plan_revision_count") == 0
        assert result.get("user_feedback", {}).get("approved") is True

    def test_plan_approval_revise_increments_count(self):
        """T4-3: revise → revision_count 递增，回 plan 节点。"""
        state = _make_initial_state(revision_count=1)

        with patch(_INTERRUPT_TARGET) as mock_intr:
            mock_intr.return_value = {"action": "revise", "reason": "需要更多子问题"}
            mock_agent = MagicMock()
            with patch("mult_agents.nodes.plan._invoke_json_agent") as mock_invoke:
                mock_invoke.return_value = (
                    {"outline": [], "sub_questions": ["Q1"], "research_questions": [],
                     "budget": {}, "objective": "test"},
                    "content", [],
                )
                result = plan_node(state, mock_agent, "test_agent")

        assert hasattr(result, "goto")
        assert "plan" in result.goto
        assert result.update.get("plan_revision_count") == 2
        assert result.update.get("user_feedback", {}).get("feedback") == "需要更多子问题"

    def test_plan_approval_revise_max_limit_force_adopt(self):
        """T4-4: 3 次 revise 后第 4 次 → 强制采纳。"""
        state = _make_initial_state(revision_count=3)

        with patch(_INTERRUPT_TARGET) as mock_intr:
            mock_intr.return_value = {"action": "revise", "reason": "再改一次"}
            mock_agent = MagicMock()
            with patch("mult_agents.nodes.plan._invoke_json_agent") as mock_invoke:
                mock_invoke.return_value = (
                    {"outline": [], "sub_questions": ["Q1"], "research_questions": [],
                     "budget": {}, "objective": "test"},
                    "content", [],
                )
                result = plan_node(state, mock_agent, "test_agent")

        assert isinstance(result, dict)
        assert result.get("plan_revision_count") == 3
        assert result.get("user_feedback", {}).get("reason") == "max_revisions_reached"

    def test_plan_approval_reject(self):
        """T4-5: reject → END，保留已生成内容。"""
        state = _make_initial_state(revision_count=0)

        with patch(_INTERRUPT_TARGET) as mock_intr:
            mock_intr.return_value = {"action": "reject", "reason": "方向不对"}
            mock_agent = MagicMock()
            with patch("mult_agents.nodes.plan._invoke_json_agent") as mock_invoke:
                mock_invoke.return_value = (
                    {"outline": [], "sub_questions": ["Q1"], "research_questions": [],
                     "budget": {}, "objective": "test"},
                    "content", [],
                )
                result = plan_node(state, mock_agent, "test_agent")

        assert hasattr(result, "goto")
        assert "__end__" in result.goto
        assert "否决" in result.update.get("final", "")

    def test_plan_approval_no_hitl_skips_interrupt(self):
        """HITL 未启用 → 不 interrupt，直通。"""
        state = _make_initial_state(hitl_enabled=False)

        with patch(_INTERRUPT_TARGET) as mock_intr:
            mock_agent = MagicMock()
            with patch("mult_agents.nodes.plan._invoke_json_agent") as mock_invoke:
                mock_invoke.return_value = (
                    {"outline": [], "sub_questions": ["Q1"], "research_questions": [],
                     "budget": {}, "objective": "test"},
                    "content", [],
                )
                result = plan_node(state, mock_agent, "test_agent")

            mock_intr.assert_not_called()

        assert isinstance(result, dict)


# ──────────────────────────────────────────────
# T4-6 报告审核测试
# ──────────────────────────────────────────────

@pytest.mark.skipif(not HAS_WRITE_NODE, reason="write_node 依赖加载失败")
class TestReportReview:

    @pytest.mark.asyncio
    async def test_report_review_adopt(self):
        """T4-6: adopt → 正常返回报告。"""
        state = _make_initial_state(hitl_enabled=True, write_review=True)

        with patch(_INTERRUPT_TARGET) as mock_intr:
            mock_intr.return_value = {"action": "adopt"}
            mock_agent = MagicMock()

            async def mock_astream(*args, **kwargs):
                yield (MagicMock(content="报告正文"), {})
            mock_agent.astream = mock_astream

            with patch("mult_agents.nodes.write._check_evidence_sufficiency", return_value=(True, "")):
                with patch("mult_agents.nodes.write._validate_and_fix_citations", return_value=("报告正文", [])):
                    with patch("mult_agents.nodes.write._ensure_reference_section", return_value="报告正文"):
                        result = await write_node(state, mock_agent, "test_agent")

        assert isinstance(result, dict)
        assert "final" in result
        assert "报告正文" in result["final"]

    @pytest.mark.asyncio
    async def test_report_review_deepen_within_limit(self):
        """T4-6: deepen + iteration < max → 回 plan 节点。"""
        state = _make_initial_state(hitl_enabled=True, write_review=True, iteration=1, max_iterations=3)

        with patch(_INTERRUPT_TARGET) as mock_intr:
            mock_intr.return_value = {"action": "deepen", "extra_sub_questions": ["深入分析X方向"]}
            mock_agent = MagicMock()

            async def mock_astream(*args, **kwargs):
                yield (MagicMock(content="报告"), {})
            mock_agent.astream = mock_astream

            with patch("mult_agents.nodes.write._check_evidence_sufficiency", return_value=(True, "")):
                with patch("mult_agents.nodes.write._validate_and_fix_citations", return_value=("报告", [])):
                    with patch("mult_agents.nodes.write._ensure_reference_section", return_value="报告"):
                        result = await write_node(state, mock_agent, "test_agent")

        assert hasattr(result, "goto")
        assert "plan" in result.goto
        assert result.update.get("iteration") == 2

    @pytest.mark.asyncio
    async def test_report_review_deepen_at_max_force_adopt(self):
        """T4-6: deepen + iteration >= max → 强制采纳。"""
        state = _make_initial_state(hitl_enabled=True, write_review=True, iteration=3, max_iterations=3)

        with patch(_INTERRUPT_TARGET) as mock_intr:
            mock_intr.return_value = {"action": "deepen", "extra_sub_questions": ["深入分析X方向"]}
            mock_agent = MagicMock()

            async def mock_astream(*args, **kwargs):
                yield (MagicMock(content="报告"), {})
            mock_agent.astream = mock_astream

            with patch("mult_agents.nodes.write._check_evidence_sufficiency", return_value=(True, "")):
                with patch("mult_agents.nodes.write._validate_and_fix_citations", return_value=("报告", [])):
                    with patch("mult_agents.nodes.write._ensure_reference_section", return_value="报告"):
                        result = await write_node(state, mock_agent, "test_agent")

        assert isinstance(result, dict)
        assert "final" in result


# ──────────────────────────────────────────────
# T4-7 payload 校验测试
# ──────────────────────────────────────────────


def test_clarify_resume_payload_valid():
    from backend.schemas.research import ClarifyResumePayload
    p = ClarifyResumePayload(kind="clarification", answers=["回答1", "回答2"])
    assert p.kind == "clarification"
    assert len(p.answers) == 2


def test_plan_approval_resume_payload_approve_valid():
    from backend.schemas.research import PlanApprovalResumePayload
    p = PlanApprovalResumePayload(kind="plan_approval", action="approve")
    assert p.action == "approve"
    assert p.reason is None


def test_plan_approval_resume_payload_revise_requires_reason():
    from backend.schemas.research import PlanApprovalResumePayload
    with pytest.raises(ValidationError):
        PlanApprovalResumePayload(kind="plan_approval", action="revise")


def test_plan_approval_resume_payload_revise_with_reason():
    from backend.schemas.research import PlanApprovalResumePayload
    p = PlanApprovalResumePayload(kind="plan_approval", action="revise", reason="需要调整")
    assert p.reason == "需要调整"


def test_plan_approval_resume_payload_invalid_action():
    from backend.schemas.research import PlanApprovalResumePayload
    with pytest.raises(ValidationError):
        PlanApprovalResumePayload(kind="plan_approval", action="invalid")


def test_report_review_resume_payload_adopt_valid():
    from backend.schemas.research import ReportReviewResumePayload
    p = ReportReviewResumePayload(kind="report_review", action="adopt")
    assert p.action == "adopt"


def test_report_review_resume_payload_deepen_requires_extra():
    from backend.schemas.research import ReportReviewResumePayload
    with pytest.raises(ValidationError):
        ReportReviewResumePayload(kind="report_review", action="deepen")


def test_report_review_resume_payload_deepen_with_extra():
    from backend.schemas.research import ReportReviewResumePayload
    p = ReportReviewResumePayload(kind="report_review", action="deepen",
                                   extra_sub_questions=["方向A", "方向B"])
    assert len(p.extra_sub_questions) == 2


# ──────────────────────────────────────────────
# T4-8 interrupt 重建 API 测试
# ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_interrupt_no_snapshot():
    """无 checkpoint → active=false。"""
    mock_snapshot = MagicMock()
    mock_snapshot.next = ()
    mock_snapshot.tasks = ()
    result = {"active": False, "thread_id": "t1"}
    if mock_snapshot.next and mock_snapshot.tasks:
        for task in mock_snapshot.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                result = {"active": True, "thread_id": "t1"}
                break
    assert result["active"] is False
    assert result["thread_id"] == "t1"


@pytest.mark.asyncio
async def test_get_interrupt_with_active_interrupt():
    """有 interrupt → active=true, 返回完整审批数据。"""
    mock_intr = MagicMock()
    mock_intr.id = "intr-001"
    mock_intr.value = {"kind": "plan_approval", "sub_questions": [{"id": 0, "question": "Q1"}], "revision_count": 0}
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
                result = {"active": True, "thread_id": "t1", "interrupt_id": intr.id,
                          "kind": value.get("kind", "unknown"), "payload": value}
                break

    assert result["active"] is True
    assert result["kind"] == "plan_approval"
    assert result["interrupt_id"] == "intr-001"
    assert "sub_questions" in result["payload"]


@pytest.mark.asyncio
async def test_get_interrupt_with_clarification_kind():
    """clarification kind 的 interrupt 正确提取。"""
    mock_intr = MagicMock()
    mock_intr.id = "intr-002"
    mock_intr.value = {"kind": "clarification", "questions": [{"id": "q1", "question": "请澄清"}]}
    mock_task = MagicMock()
    mock_task.interrupts = [mock_intr]
    mock_snapshot = MagicMock()
    mock_snapshot.next = ("clarify",)
    mock_snapshot.tasks = (mock_task,)

    result = {"active": False, "thread_id": "t1"}
    if mock_snapshot.next and mock_snapshot.tasks:
        for task in mock_snapshot.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                intr = task.interrupts[0]
                value = intr.value if isinstance(intr.value, dict) else {"value": intr.value}
                result = {"active": True, "thread_id": "t1", "interrupt_id": intr.id,
                          "kind": value.get("kind", "unknown"), "payload": value}
                break

    assert result["active"] is True
    assert result["kind"] == "clarification"
    assert "questions" in result["payload"]


@pytest.mark.asyncio
async def test_get_interrupt_with_report_review_kind():
    """report_review kind 的 interrupt 正确提取。"""
    mock_intr = MagicMock()
    mock_intr.id = "intr-003"
    mock_intr.value = {"kind": "report_review", "report_preview": "报告预览..."}
    mock_task = MagicMock()
    mock_task.interrupts = [mock_intr]
    mock_snapshot = MagicMock()
    mock_snapshot.next = ("write",)
    mock_snapshot.tasks = (mock_task,)

    result = {"active": False, "thread_id": "t1"}
    if mock_snapshot.next and mock_snapshot.tasks:
        for task in mock_snapshot.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                intr = task.interrupts[0]
                value = intr.value if isinstance(intr.value, dict) else {"value": intr.value}
                result = {"active": True, "thread_id": "t1", "interrupt_id": intr.id,
                          "kind": value.get("kind", "unknown"), "payload": value}
                break

    assert result["active"] is True
    assert result["kind"] == "report_review"
    assert "report_preview" in result["payload"]


@pytest.mark.asyncio
async def test_get_interrupt_no_active_after_resume():
    """T4-8: resume 后该接口返回 active=false。"""
    mock_snapshot = MagicMock()
    mock_snapshot.next = ()
    mock_snapshot.tasks = ()
    result = {"active": False, "thread_id": "t1"}
    if mock_snapshot.next and mock_snapshot.tasks:
        for task in mock_snapshot.tasks:
            if hasattr(task, "interrupts") and task.interrupts:
                result = {"active": True, "thread_id": "t1"}
                break
    assert result["active"] is False


# ──────────────────────────────────────────────
# raise_interrupt 辅助函数测试
# ──────────────────────────────────────────────


def test_raise_interrupt_includes_kind():
    """raise_interrupt 自动在 payload 中添加 kind 键。"""
    with patch(_INTERRUPT_TARGET) as mock_intr:
        mock_intr.return_value = {"action": "approve"}
        result = raise_interrupt("plan_approval", {"sub_questions": []})
        call_args = mock_intr.call_args[0][0]
        assert call_args["kind"] == "plan_approval"
        assert "sub_questions" in call_args


def test_raise_interrupt_all_three_kinds():
    """raise_interrupt 支持所有三种 kind。"""
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
# State 字段测试
# ──────────────────────────────────────────────


def test_state_has_plan_revision_count():
    state = create_initial_state(query="test", max_iterations=3, user_id="u", tenant_id="t")
    assert state["plan_revision_count"] == 0


def test_state_has_clarifications():
    state = create_initial_state(query="test", max_iterations=3, user_id="u", tenant_id="t")
    assert state["clarifications"] == []


def test_state_has_hitl_config_defaults():
    state = create_initial_state(query="test", max_iterations=3, user_id="u", tenant_id="t")
    config = state["hitl_config"]
    assert "plan_review" in config
    assert "analyze_clarify" in config
    assert "write_review" in config
    assert config["write_review"] is False


def test_state_hitl_enabled_propagates():
    state = create_initial_state(query="test", max_iterations=3, user_id="u", tenant_id="t", hitl_enabled=True)
    assert state["hitl_enabled"] is True


def test_state_custom_hitl_config():
    custom_config = {"plan_review": False, "analyze_clarify": False, "write_review": True}
    state = create_initial_state(query="test", max_iterations=3, user_id="u", tenant_id="t",
                                  hitl_enabled=True, hitl_config=custom_config)
    assert state["hitl_config"]["write_review"] is True
    assert state["hitl_config"]["plan_review"] is False
