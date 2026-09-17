"""resume_stream 三种 mode 的执行输入构造。

三种 mode 复用同一 checkpoint 的 state，差别只在传给 astream 的输入：
    continue → None（从断点节点续跑）
    answer   → Command(resume=...)（从 interrupt() 处继续）
    modify   → Command(update=..., goto="intent")（追加消息后从入口重跑）

本文件锁定「输入构造」这一层：分支选错或漏实现会直接暴露为 NameError/类型错误，
比按源码文本断言更能抓住真实风险。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_resume_modes.py -v
"""

from types import SimpleNamespace

import pytest

from backend.service import research_service as rs
from backend.service.research_service import ResearchService


class _FakeApp:
    """graph 替身：只记录 astream 收到的输入，不产生任何 chunk。"""

    def __init__(self):
        self.astream_calls = []

    async def astream(self, input_state, config=None, stream_mode=None):
        self.astream_calls.append(input_state)
        if False:
            yield ("updates", {})

    async def aget_state(self, config=None):
        return SimpleNamespace(
            values={"final": ""}, next=(), interrupts=[], tasks=(),
        )

    async def aupdate_state(self, config, values, as_node=None):
        return SimpleNamespace(config=config, values=values)


async def _noop(*args, **kwargs):
    return None


async def _collect(service, **kwargs):
    return [frame async for frame in service.resume_stream(**kwargs)]


@pytest.fixture
def service(monkeypatch):
    """构造不触发图初始化的 ResearchService，并隔离所有外部副作用。"""
    svc = ResearchService()
    fake_app = _FakeApp()
    svc._app = fake_app
    svc._initialized = True

    monkeypatch.setattr(rs, "get_research_logger", lambda thread_id: None)
    monkeypatch.setattr(rs, "close_research_logger", lambda *a, **k: None)
    monkeypatch.setattr(ResearchService, "_apply_summary_to_checkpoint", _noop)
    monkeypatch.setattr(ResearchService, "_complete_thread", _noop)
    monkeypatch.setattr(ResearchService, "_trigger_memory_extract_from_snapshot", _noop)

    return svc, fake_app


# ── mode=continue ──


class TestContinueMode:
    @pytest.mark.asyncio
    async def test_continue_passes_none_input(self, service):
        """continue 用 None 输入：LangGraph 从最后 checkpoint 的节点续跑。"""
        svc, app = service

        await _collect(svc, thread_id="t1", mode="continue")

        assert app.astream_calls == [None]


# ── mode=answer ──


class TestAnswerMode:
    @pytest.mark.asyncio
    async def test_answer_wraps_resume_value(self, service):
        """answer 用 Command(resume=...)：interrupt() 的返回值即用户回答。"""
        svc, app = service
        payload = {"kind": "plan_approval", "action": "approve"}

        await _collect(svc, thread_id="t1", mode="answer", resume_value=payload)

        cmd = app.astream_calls[0]
        assert cmd.resume == payload
        assert not cmd.goto

    @pytest.mark.asyncio
    async def test_answer_without_value_reports_invalid_resume(self, service):
        """answer 缺 resume_value → InvalidResume，且不触碰 graph。"""
        svc, app = service

        frames = await _collect(svc, thread_id="t1", mode="answer")

        assert "InvalidResume" in "".join(frames)
        assert app.astream_calls == []


# ── mode=modify ──


class TestModifyMode:
    @pytest.mark.asyncio
    async def test_modify_returns_to_intent_with_new_query(self, service):
        """modify 用 Command(goto="intent")：新条件覆盖 query，流程从入口重算。"""
        svc, app = service

        await _collect(svc, thread_id="t1", mode="modify", resume_value="只关注汽车行业")

        cmd = app.astream_calls[0]
        assert cmd.goto == "intent"
        assert cmd.update["query"] == "只关注汽车行业"
        assert getattr(cmd.update["chat_messages"][0], "content") == "只关注汽车行业"

    @pytest.mark.asyncio
    async def test_modify_resets_overwrite_fields(self, service):
        """上一轮的 plan/final/draft/iteration 不应带进新一轮。"""
        svc, app = service

        await _collect(svc, thread_id="t1", mode="modify", resume_value="换个角度")

        update = app.astream_calls[0].update
        assert update["plan"] == ""
        assert update["final"] == ""
        assert update["draft"] == ""
        assert update["iteration"] == 0
        assert update["needs_more_research"] is False

    @pytest.mark.asyncio
    async def test_modify_without_text_reports_invalid_resume(self, service):
        """modify 缺补充文本 → InvalidResume，且不触碰 graph。"""
        svc, app = service

        frames = await _collect(svc, thread_id="t1", mode="modify")

        assert "InvalidResume" in "".join(frames)
        assert app.astream_calls == []


# ── 兜底 ──


class TestUnknownMode:
    @pytest.mark.asyncio
    async def test_unknown_mode_reports_invalid_resume(self, service):
        """schema 已拦截非法 mode，service 层仍做最后一道兜底。"""
        svc, app = service

        frames = await _collect(svc, thread_id="t1", mode="bogus")

        assert "InvalidResume" in "".join(frames)
        assert app.astream_calls == []
