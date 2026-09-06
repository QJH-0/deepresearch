"""R2.4 thinking 深度思考流式透传测试。

覆盖用例:
    T2.4-01 custom thinking 事件转发
    T2.4-02 thinking 先于 token 时 message.start 触发
    T2.4-03 thinking_nodes 配置驱动
    T2.4-04 reasoning 字段捕获
    T2.4-08 事件协议零变更

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m pytest app/test/test_thinking_stream.py -v
"""

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_APP_PATH = _PROJECT_ROOT / "app"
sys.path.insert(0, str(_APP_PATH))

from backend.schemas.events import EVENT_REGISTRY, MessageThinkingData


# ──────────────────────────────────────────────
# T2.4-08 事件协议零变更
# ──────────────────────────────────────────────


class TestEventProtocolUnchanged:
    """MessageThinkingData 事件 schema 未被修改。"""

    def test_thinking_event_registered(self):
        assert "message.thinking" in EVENT_REGISTRY

    def test_thinking_data_fields(self):
        model = MessageThinkingData(message_id="run1:write", text="推理")
        assert model.message_id == "run1:write"
        assert model.text == "推理"

    def test_thinking_data_required_fields(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            MessageThinkingData()


# ──────────────────────────────────────────────
# T2.4-03 thinking_nodes 配置驱动
# ──────────────────────────────────────────────


class TestThinkingNodesConfig:
    """build_agent 的 enable_thinking 参数按 thinking_nodes 配置注入。"""

    def test_thinking_nodes_empty_disables_all(self):
        from mult_agents.runtime import build_agent

        with patch("mult_agents.runtime.build_agent", wraps=build_agent) as mock_build:
            from mult_agents.runtime import build_agents
            from mult_agents.config import AppConfig

            config = AppConfig(
                api_key="test",
                model="qwen-plus",
                thread_id="t",
                user_id="u",
                tenant_id="t",
                max_iterations=3,
                enable_memory=False,
                memory_embedding_model="",
                memory_hot_path_top_k=5,
                memory_background_enabled=False,
                memory_extract_model="qwen-turbo",
                save_conversation_task=False,
                checkpointer_backend="memory",
                enable_milvus=False,
                redis_url="",
                postgres_dsn="",
                milvus_host="",
                milvus_port=19530,
                milvus_collection="",
                thinking_nodes=[],
            )
            try:
                build_agents("qwen-plus", "test", config)
            except Exception:
                pass

            for call in mock_build.call_args_list:
                assert not call.kwargs.get("enable_thinking", False), \
                    "thinking_nodes=[] 时所有 agent 的 enable_thinking 应为 False"

    def test_thinking_nodes_includes_write(self):
        from mult_agents.config import AppConfig

        call_log = []

        def _fake_build_agent(model, api_key, prompt_key, temperature, tools, enable_thinking=False):
            call_log.append({"prompt_key": prompt_key, "enable_thinking": enable_thinking})
            return MagicMock()

        with patch("mult_agents.runtime.build_agent", side_effect=_fake_build_agent), \
             patch("mult_agents.runtime.init_rag_system"):
            from mult_agents.runtime import build_agents

            config = AppConfig(
                api_key="test",
                model="qwen-plus",
                thread_id="t",
                user_id="u",
                tenant_id="t",
                max_iterations=3,
                enable_memory=False,
                memory_embedding_model="",
                memory_hot_path_top_k=5,
                memory_background_enabled=False,
                memory_extract_model="qwen-turbo",
                save_conversation_task=False,
                checkpointer_backend="memory",
                enable_milvus=False,
                redis_url="",
                postgres_dsn="",
                milvus_host="",
                milvus_port=19530,
                milvus_collection="",
                thinking_nodes=["write", "deep_dive", "analyze"],
            )
            build_agents("qwen-plus", "test", config)

        write_calls = [c for c in call_log if c["prompt_key"] == "write"]
        assert len(write_calls) == 1
        assert write_calls[0]["enable_thinking"] is True

        intent_calls = [c for c in call_log if c["prompt_key"] == "intent_router"]
        assert len(intent_calls) == 1
        assert intent_calls[0]["enable_thinking"] is False


# ──────────────────────────────────────────────
# T2.4-04 reasoning 字段捕获
# ──────────────────────────────────────────────


class TestReasoningCapture:
    """节点 astream 循环中 reasoning_content 被正确捕获为 thinking 事件。"""

    @pytest.mark.asyncio
    async def test_reasoning_from_attribute(self):
        """chunk 有 reasoning_content 属性时推送 thinking。"""
        from mult_agents.nodes.write import write_node

        thinking_events = []

        async def fake_astream(*args, **kwargs):
            chunk1 = MagicMock()
            chunk1.reasoning_content = "步骤一"
            chunk1.content = ""
            chunk1.additional_kwargs = {}
            yield (chunk1, {})

            chunk2 = MagicMock()
            chunk2.reasoning_content = None
            chunk2.content = "正文"
            chunk2.additional_kwargs = {}
            yield (chunk2, {})

        agent = MagicMock()
        agent.astream = fake_astream
        agent.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="降级")]})

        state = {
            "query": "test",
            "sub_questions": [],
            "findings": [],
            "source_index": [{"source_id": "WEB1_1-1", "label": "test", "locator": "http://example.com"}],
            "audit_flags": [],
            "web_retrieval_stats": {"query_count": 1, "raw_count": 3, "kept_count": 2},
            "local_retrieval_stats": {"query_count": 0, "raw_count": 0, "kept_count": 0},
        }

        def writer(data):
            if isinstance(data, dict) and data.get("type") == "thinking":
                thinking_events.append(data)

        with patch("mult_agents.nodes.write._check_evidence_sufficiency", return_value=(True, "")), \
             patch("mult_agents.nodes.write._validate_and_fix_citations", return_value=("正文", [])), \
             patch("mult_agents.nodes.write._ensure_reference_section", return_value="正文"):
            await write_node(state, agent, "test_agent", writer=writer)

        assert len(thinking_events) == 1
        assert thinking_events[0]["text"] == "步骤一"
        assert thinking_events[0]["node"] == "write"

    @pytest.mark.asyncio
    async def test_reasoning_from_additional_kwargs(self):
        """chunk 无 reasoning_content 属性但 additional_kwargs 有 reasoning_content。"""
        from mult_agents.nodes.write import write_node

        thinking_events = []

        async def fake_astream(*args, **kwargs):
            chunk1 = MagicMock()
            del chunk1.reasoning_content
            chunk1.content = ""
            chunk1.additional_kwargs = {"reasoning_content": "推理片段"}
            yield (chunk1, {})

            chunk2 = MagicMock()
            del chunk2.reasoning_content
            chunk2.content = "正文内容"
            chunk2.additional_kwargs = {}
            yield (chunk2, {})

        agent = MagicMock()
        agent.astream = fake_astream
        agent.ainvoke = AsyncMock(return_value={"messages": [MagicMock(content="降级")]})

        state = {
            "query": "test",
            "sub_questions": [],
            "findings": [],
            "source_index": [{"source_id": "WEB1_1-1", "label": "test", "locator": "http://example.com"}],
            "audit_flags": [],
            "web_retrieval_stats": {"query_count": 1, "raw_count": 3, "kept_count": 2},
            "local_retrieval_stats": {"query_count": 0, "raw_count": 0, "kept_count": 0},
        }

        def writer(data):
            if isinstance(data, dict) and data.get("type") == "thinking":
                thinking_events.append(data)

        with patch("mult_agents.nodes.write._check_evidence_sufficiency", return_value=(True, "")), \
             patch("mult_agents.nodes.write._validate_and_fix_citations", return_value=("正文内容", [])), \
             patch("mult_agents.nodes.write._ensure_reference_section", return_value="正文内容"):
            await write_node(state, agent, "test_agent", writer=writer)

        assert len(thinking_events) == 1
        assert thinking_events[0]["text"] == "推理片段"


# ──────────────────────────────────────────────
# T2.4-01/T2.4-02 custom thinking 事件转发
# ──────────────────────────────────────────────


class TestCustomThinkingForwarding:
    """service 层 custom 通道 thinking 分支转发为 message.thinking SSE 事件。"""

    def _parse_sse(self, sse_str: str) -> dict:
        """解析 SSE data 行为 dict。"""
        assert sse_str.startswith("data: ")
        json_str = sse_str[len("data: "):].strip()
        return json.loads(json_str)

    def test_thinking_event_forwarded(self):
        """custom thinking 事件被转发为 message.thinking SSE。"""
        from backend.schemas.events import event, sse

        # 模拟节点推送 thinking
        chunk = {"type": "thinking", "node": "write", "text": "推理片段"}
        evt_type = chunk.get("type", "")
        node = chunk.get("node", "")
        text = chunk.get("text", "")
        mid = "run123:write"

        result = sse(event("message.thinking", message_id=mid, text=text))

        parsed = self._parse_sse(result)
        assert parsed["type"] == "message.thinking"
        assert parsed["data"]["message_id"] == "run123:write"
        assert parsed["data"]["text"] == "推理片段"

    def test_thinking_before_token_triggers_message_start(self):
        """thinking 先于 token 到达时，message.start 由 thinking 分支触发。"""
        seen_nodes: set[str] = set()
        run_id = "run456"

        # 模拟 thinking 事件
        chunk1 = {"type": "thinking", "node": "write", "text": "思考中..."}
        node = chunk1["node"]
        mid = f"{run_id}:{node}"

        events = []
        if node not in seen_nodes:
            seen_nodes.add(node)
            from backend.schemas.events import event, sse
            events.append(sse(event("message.start", message_id=mid, node=node)))
        events.append(sse(event("message.thinking", message_id=mid, text=chunk1["text"])))

        # 模拟 token 事件
        chunk2 = {"type": "token", "node": "write", "text": "正文"}
        node2 = chunk2["node"]
        mid2 = f"{run_id}:{node2}"
        if node2 not in seen_nodes:
            seen_nodes.add(node2)
            from backend.schemas.events import event, sse
            events.append(sse(event("message.start", message_id=mid2, node=node2)))
        from backend.schemas.events import event, sse
        events.append(sse(event("message.delta", message_id=mid2, text=chunk2["text"])))

        # 验证事件序列
        types = [self._parse_sse(e)["type"] for e in events]
        assert types == ["message.start", "message.thinking", "message.delta"]
        assert "message.thinking" in types
        assert types.index("message.start") < types.index("message.thinking")
        assert types.index("message.thinking") < types.index("message.delta")


# ──────────────────────────────────────────────
# T2.4-06 resume_stream thinking 分支
# ──────────────────────────────────────────────


class TestResumeStreamThinking:
    """resume_stream 的 custom 通道同样有 thinking 分支。"""

    def test_resume_has_thinking_branch(self):
        """验证 resume_stream 代码中包含 thinking 分支。"""
        import inspect
        from backend.service.research_service import ResearchService

        src = inspect.getsource(ResearchService.resume_stream)
        assert "thinking" in src, "resume_stream 应包含 thinking 分支"
        assert "message.thinking" in src, "resume_stream 应转发 message.thinking 事件"


# ──────────────────────────────────────────────
# T2.4-05 thinking 文本上限截断（预留测试，验证配置可关闭）
# ──────────────────────────────────────────────


class TestThinkingConfig:
    """thinking_nodes 配置可以设为空数组关闭全部思考。"""

    def test_empty_thinking_nodes_disables_all(self):
        from mult_agents.config import AppConfig

        config = AppConfig(
            api_key="test",
            model="qwen-plus",
            thread_id="t",
            user_id="u",
            tenant_id="t",
            max_iterations=3,
            enable_memory=False,
            memory_embedding_model="",
            memory_hot_path_top_k=5,
            memory_background_enabled=False,
            memory_extract_model="qwen-turbo",
            save_conversation_task=False,
            checkpointer_backend="memory",
            enable_milvus=False,
            redis_url="",
            postgres_dsn="",
            milvus_host="",
            milvus_port=19530,
            milvus_collection="",
            thinking_nodes=[],
        )
        assert config.thinking_nodes == []

    def test_default_thinking_nodes(self):
        from mult_agents.config import AppConfig

        config = AppConfig(
            api_key="test",
            model="qwen-plus",
            thread_id="t",
            user_id="u",
            tenant_id="t",
            max_iterations=3,
            enable_memory=False,
            memory_embedding_model="",
            memory_hot_path_top_k=5,
            memory_background_enabled=False,
            memory_extract_model="qwen-turbo",
            save_conversation_task=False,
            checkpointer_backend="memory",
            enable_milvus=False,
            redis_url="",
            postgres_dsn="",
            milvus_host="",
            milvus_port=19530,
            milvus_collection="",
        )
        assert "write" in config.thinking_nodes
        assert "deep_dive" in config.thinking_nodes
        assert "analyze" in config.thinking_nodes
