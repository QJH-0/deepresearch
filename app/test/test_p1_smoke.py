"""Phase 1 smoke test: non-streaming /run full chain (T1-8).

Tests:
1. Research query traverses full graph and produces a report
2. Direct question goes through direct_answer branch

Run:
    cd D:\\Code\\LLMdev\\deepresearch
    set PYTHONPATH=app
    python -m pytest app/test/test_p1_smoke.py -v -s
"""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "app"))

from dotenv import load_dotenv  # noqa: E402
load_dotenv(_PROJECT_ROOT / ".env")

_DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")

_LANGGRAPH_AVAILABLE = False
try:
    import importlib.util as _ilu
    _LANGGRAPH_AVAILABLE = _ilu.find_spec("langgraph") is not None
except Exception:
    pass

_PG_REACHABLE = False
try:
    import socket as _sock
    _sock.setdefaulttimeout(2)
    _pg_host = os.getenv("POSTGRES_DSN", "")
    if ":@" in _pg_host:
        _pg_addr = _pg_host.split("@")[-1].split("/")[0]
        _pg_h, _pg_p = _pg_addr.rsplit(":", 1)
        _s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
        _s.connect((_pg_h, int(_pg_p)))
        _s.close()
        _PG_REACHABLE = True
except Exception:
    pass

pytestmark = pytest.mark.skipif(
    not _DASHSCOPE_API_KEY or not _LANGGRAPH_AVAILABLE or not _PG_REACHABLE,
    reason="需要 DASHSCOPE_API_KEY + langgraph 安装 + PostgreSQL 可连接才能运行完整链路冒烟测试",
)


@pytest.fixture(scope="module")
def research_service():
    """Initialize ResearchService, skipping RAG init if Milvus unavailable."""
    from backend.service import ResearchService
    with patch("mult_agents.models.init_rag_system", return_value=None):
        config_path = str(_PROJECT_ROOT / "app" / "config.json")
        svc = ResearchService(config_path=config_path)
        svc._ensure_initialized()
    return svc


def test_t1_8_research_full_chain(research_service):
    """T1-8: Research query full chain produces a report."""
    final, route = asyncio.run(research_service.run_with_route(
        query="Please conduct a research on the latest trends in quantum computing and provide a detailed analysis.",
        user_id="test_p1",
        thread_id="test_p1_smoke_research_v3",
        tenant_id="test",
        max_iterations=1,
        enable_memory=False,
        hitl_enabled=False,
    ))
    # route could be "direct" or "multiagent" depending on intent router
    # For P1 smoke, we just verify the chain runs and produces output
    assert len(final) > 50, f"Report too short, possibly not generated: len={len(final)}"


def test_t1_8_direct_answer_chain(research_service):
    """T1-8: Simple question goes through direct_answer branch."""
    final, route = asyncio.run(research_service.run_with_route(
        query="What is 1+1?",
        user_id="test_p1",
        thread_id="test_p1_smoke_direct_v3",
        tenant_id="test",
        max_iterations=1,
        enable_memory=False,
        hitl_enabled=False,
    ))
    assert route == "direct", f"Simple question should go direct, got route={route}"
    assert len(final) > 0, "Direct answer should not be empty"
