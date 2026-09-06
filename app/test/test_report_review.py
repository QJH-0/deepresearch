"""R4.3 协议对齐与残留清理 测试用例。

T4.3-01 ~ T4.3-06 覆盖：
- reject 载荷校验通过
- write 节点 reject 分支流转
- 残留清理无悬空引用
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


class TestReportReviewRejectSchema:
    """T4.3-01 reject 载荷校验通过。"""

    def test_adopt_passes(self):
        from backend.schemas.research import ReportReviewResumePayload
        p = ReportReviewResumePayload(kind="report_review", action="adopt")
        assert p.action == "adopt"

    def test_deepen_with_sub_questions_passes(self):
        from backend.schemas.research import ReportReviewResumePayload
        p = ReportReviewResumePayload(
            kind="report_review", action="deepen",
            extra_sub_questions=["q1", "q2"],
        )
        assert p.action == "deepen"
        assert p.extra_sub_questions == ["q1", "q2"]

    def test_reject_with_feedback_passes(self):
        from backend.schemas.research import ReportReviewResumePayload
        p = ReportReviewResumePayload(
            kind="report_review", action="reject",
            feedback="数据支撑不足",
        )
        assert p.action == "reject"
        assert p.feedback == "数据支撑不足"

    def test_reject_without_feedback_fails(self):
        from backend.schemas.research import ReportReviewResumePayload
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ReportReviewResumePayload(kind="report_review", action="reject")

    def test_invalid_action_fails(self):
        from backend.schemas.research import ReportReviewResumePayload
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            ReportReviewResumePayload(kind="report_review", action="invalid")


class TestReportReviewRejectBranch:
    """T4.3-02 write 节点 reject 分支流转。"""

    @pytest.mark.asyncio
    async def test_reject_returns_command_goto_plan(self):
        """reject 分支应返回 Command(goto='plan') 并注入 user_feedback。"""
        from langgraph.types import Command
        from mult_agents.nodes.write import write_node

        state = {
            "query": "测试查询",
            "hitl_enabled": True,
            "hitl_config": {"write_review": True},
            "iteration": 0,
            "max_iterations": 3,
        }
        agent = MagicMock()
        # astream 返回 async generator
        async def mock_astream(*a, **kw):
            yield (MagicMock(content="报告内容", reasoning_content=None, additional_kwargs={}), {})
        agent.astream = mock_astream
        agent.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="报告内容")]})

        writer = MagicMock()

        with patch("mult_agents.nodes.write.raise_interrupt") as mock_ri:
            mock_ri.return_value = {
                "action": "reject",
                "feedback": "报告质量不足",
            }
            with patch("mult_agents.nodes.write._check_evidence_sufficiency", return_value=(True, "")):
                with patch("mult_agents.nodes.write._validate_and_fix_citations", return_value=("报告内容", set())):
                    with patch("mult_agents.nodes.write._ensure_reference_section", return_value="报告内容"):
                        result = await write_node(state, agent, "write", writer)

        assert isinstance(result, Command)
        assert result.goto == "plan"
        assert result.update.get("user_feedback") == "报告质量不足"
        assert result.update.get("iteration") == 1

    @pytest.mark.asyncio
    async def test_reject_at_max_iteration_adopts(self):
        """迭代上限时 reject 直接采纳。"""
        from mult_agents.nodes.write import write_node

        state = {
            "query": "测试查询",
            "hitl_enabled": True,
            "hitl_config": {"write_review": True},
            "iteration": 3,
            "max_iterations": 3,
        }
        agent = MagicMock()
        async def mock_astream(*a, **kw):
            yield (MagicMock(content="报告内容", reasoning_content=None, additional_kwargs={}), {})
        agent.astream = mock_astream
        agent.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="报告内容")]})

        writer = MagicMock()

        with patch("mult_agents.nodes.write.raise_interrupt") as mock_ri:
            mock_ri.return_value = {
                "action": "reject",
                "feedback": "报告质量不足",
            }
            with patch("mult_agents.nodes.write._check_evidence_sufficiency", return_value=(True, "")):
                with patch("mult_agents.nodes.write._validate_and_fix_citations", return_value=("报告内容", set())):
                    with patch("mult_agents.nodes.write._ensure_reference_section", return_value="报告内容"):
                        result = await write_node(state, agent, "write", writer)

        assert isinstance(result, dict)
        assert "final" in result


async def async_gen(items):
    for item in items:
        yield item


class TestMemoryShellCleaned:
    """T4.3-05 残留清理无悬空引用。"""

    def test_no_mult_agents_memory_imports(self):
        """业务代码无 from mult_agents.memory 引用。"""
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "-c",
             "import subprocess; "
             "r = subprocess.run(['git', '-C', '.', 'grep', '-rn', '-E', r'from mult_agents\\.memory', '--', 'app/', 'agent_front/src/'], "
             "capture_output=True, text=True); "
             "print(r.stdout.strip() or 'CLEAN')"],
            capture_output=True, text=True, cwd=".",
        )
        output = result.stdout.strip()
        lines = [l for l in output.splitlines()
                 if l and "CLEAN" not in l and "__pycache__" not in l
                 and "/test/" not in l]
        assert len(lines) == 0, f"业务代码仍有 mult_agents.memory 引用: {lines}"

    def test_memory_directory_removed(self):
        """mult_agents/memory 目录已移除。"""
        from pathlib import Path
        memory_dir = Path(__file__).resolve().parents[1] / "mult_agents" / "memory"
        assert not memory_dir.exists(), f"{memory_dir} 应已移除"
