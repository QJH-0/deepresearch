"""T2.1 SSE 心跳保活单元测试。

覆盖：
- T2.1-01 空闲超时产出心跳
- T2.1-02 超时后不丢失/不重复 chunk
- T2.1-03 interval=0 关闭心跳
- T2.1-04 取消传播
- T2.1-05 stream_research 集成心跳帧格式
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))


async def _slow_generator(items, delays):
    """产出 items[i] 前等待 delays[i] 秒。"""
    for item, delay in zip(items, delays):
        await asyncio.sleep(delay)
        yield item


class TestHeartbeatBasic:
    """T2.1-01~03 _astream_with_heartbeat 基本行为。"""

    @pytest.mark.asyncio
    async def test_idle_timeout_produces_heartbeat(self):
        """T2.1-01 空闲超时产出心跳【P0】"""
        from backend.service.research_service import _astream_with_heartbeat

        async def gen():
            yield "custom", {"a": 1}
            await asyncio.sleep(0.05)
            yield "custom", {"b": 2}

        results = []
        async for mode, chunk in _astream_with_heartbeat(gen(), 0.02):
            results.append((mode, chunk))

        modes = [r[0] for r in results]
        assert "heartbeat" in modes
        idx_hb = modes.index("heartbeat")
        idx_a = modes.index("custom")
        assert idx_hb > idx_a
        business = [r for r in results if r[0] != "heartbeat"]
        assert len(business) == 2
        assert business[0][1] == {"a": 1}
        assert business[1][1] == {"b": 2}

    @pytest.mark.asyncio
    async def test_no_chunk_loss_or_duplication(self):
        """T2.1-02 超时后不丢失/不重复 chunk【P0】"""
        from backend.service.research_service import _astream_with_heartbeat

        async def gen():
            for i in range(10):
                await asyncio.sleep(0.03)
                yield "custom", {"idx": i}

        results = []
        async for mode, chunk in _astream_with_heartbeat(gen(), 0.01):
            results.append((mode, chunk))

        business = [r for r in results if r[0] != "heartbeat"]
        assert len(business) == 10
        for i, (_, chunk) in enumerate(business):
            assert chunk == {"idx": i}
        heartbeats = [r for r in results if r[0] == "heartbeat"]
        assert len(heartbeats) >= 9

    @pytest.mark.asyncio
    async def test_interval_zero_disables_heartbeat(self):
        """T2.1-03 interval=0 关闭心跳【P0】"""
        from backend.service.research_service import _astream_with_heartbeat

        async def gen():
            yield "custom", {"a": 1}
            await asyncio.sleep(0.05)
            yield "custom", {"b": 2}

        results = []
        async for mode, chunk in _astream_with_heartbeat(gen(), 0):
            results.append((mode, chunk))

        assert len(results) == 2
        assert all(r[0] != "heartbeat" for r in results)

    @pytest.mark.asyncio
    async def test_cancel_propagation(self):
        """T2.1-04 取消传播【P0】"""
        from backend.service.research_service import _astream_with_heartbeat

        async def gen():
            yield "custom", {"a": 1}
            await asyncio.sleep(100)

        async def collect():
            results = []
            async for mode, chunk in _astream_with_heartbeat(gen(), 0.01):
                results.append((mode, chunk))
            return results

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestHeartbeatFrameFormat:
    """T2.1-05 stream_research 集成心跳帧格式【P1】"""

    @pytest.mark.asyncio
    async def test_stream_research_heartbeat_frame(self):
        """mock astream 慢速 generator，验证 SSE 输出含心跳帧。"""
        from backend.service.research_service import ResearchService, HEARTBEAT_FRAME
        from backend.schemas.events import sse, event

        svc = ResearchService.__new__(ResearchService)
        svc._initialized = True
        svc._thread_repo = None
        svc._base_config = None

        run_started = sse(event("run.started", thread_id="t1", run_id="r1"))
        run_completed = sse(event("run.completed", message_id="r1:write", final_state="done"))

        async def slow_astream(input_state, config, stream_mode=None):
            yield "updates", {"write": {"final": "done"}}
            await asyncio.sleep(0.05)

        mock_app = MagicMock()
        mock_app.astream = slow_astream
        mock_app.aget_state = AsyncMock()
        svc._app = mock_app

        svc._build_runtime_config = MagicMock(return_value=MagicMock(
            max_iterations=3, user_id="u", tenant_id="t", thread_id="t1",
            enable_memory=False, hitl_enabled=True, hitl_config={},
            api_key="", model="qwen-plus",
        ))
        svc._apply_summary_if_needed = AsyncMock(return_value={"query": "test"})
        svc._record_thread = MagicMock()
        svc._complete_thread = MagicMock()
        svc._trigger_memory_extract = MagicMock()
        svc._trigger_title_gen = MagicMock()

        with patch("backend.service.research_service._get_heartbeat_interval", return_value=0.01):
            with patch("backend.service.research_service.get_research_logger") as mock_gl:
                mock_gl.return_value = MagicMock()
                mock_gl.close_research_logger = MagicMock()
            with patch("backend.service.research_service.close_research_logger"):
                with patch("backend.service.research_service.get_memory_service", return_value=None):
                    with patch("backend.service.research_service.get_summary_service", return_value=None):
                        outputs = []
                        async for sse_str in svc.stream_research(
                            "test", "u", "t1", "t", max_iterations=3, enable_memory=False
                        ):
                            outputs.append(sse_str)

        assert any(o == HEARTBEAT_FRAME for o in outputs)
        assert outputs[0].startswith("data:")
        assert outputs[-1].startswith("data:")
        hb_indices = [i for i, o in enumerate(outputs) if o == HEARTBEAT_FRAME]
        if hb_indices:
            assert hb_indices[0] > 0
            assert hb_indices[-1] < len(outputs) - 1
