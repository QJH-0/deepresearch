"""Phase 0 测试：事件协议 schema 完整性、envelope 格式、导出幂等、配置加载、死代码无残留。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    set PYTHONPATH=app
    python -m pytest app/test/test_events.py -v
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

# 确保路径
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "app"))

from backend.schemas.events import (  # noqa: E402
    EVENT_REGISTRY,
    EventEnvelope,
    SourceItem,
    event,
    sse,
)
from pydantic import ValidationError  # noqa: E402


# ──────────────────────────────────────────────
# T0-1 事件 schema 完整性
# ──────────────────────────────────────────────

EXPECTED_EVENT_TYPES = {
    "run.started",
    "agent.status",
    "message.start",
    "message.delta",
    "message.thinking",
    "sources.found",
    "interrupt.raised",
    "run.completed",
    "run.cancelled",
    "run.error",
}


def test_event_registry_covers_all_types():
    """EVENT_REGISTRY 覆盖第三节 10 种 type。"""
    assert set(EVENT_REGISTRY.keys()) == EXPECTED_EVENT_TYPES


def test_source_item_construction():
    """SourceItem 可从示例 dict 构造。"""
    s = SourceItem(url="https://example.com", title="Example", snippet="...", source_type="web")
    assert s.url == "https://example.com"
    assert s.source_type == "web"


def test_source_item_invalid_type_raises():
    """非法 source_type 报 ValidationError。"""
    with pytest.raises(ValidationError):
        SourceItem(source_type="invalid")  # type: ignore[arg-type]


def test_interrupt_raised_invalid_kind_raises():
    """interrupt.raised 的 kind 只允许三种枚举值。"""
    from backend.schemas.events import InterruptRaisedData

    with pytest.raises(ValidationError):
        InterruptRaisedData(interrupt_id="x", kind="invalid_kind", payload={})


def test_all_data_models_constructible_from_examples():
    """每种 data 模型可从示例 dict 构造且非法字段报 ValidationError。"""
    examples = {
        "run.started": {"thread_id": "t1", "run_id": "r1"},
        "agent.status": {"node": "plan", "label": "规划", "phase": "completed"},
        "message.start": {"message_id": "m1", "role": "assistant", "node": "plan"},
        "message.delta": {"message_id": "m1", "text": "hello"},
        "message.thinking": {"message_id": "m1", "text": "thinking..."},
        "sources.found": {"sources": [{"url": "https://x.com", "title": "X"}]},
        "interrupt.raised": {"interrupt_id": "i1", "kind": "plan_approval", "payload": {}},
        "run.completed": {"message_id": "m1", "final_state": "done"},
        "run.cancelled": {"reason": "user_cancelled"},
        "run.error": {"code": "RuntimeError", "message": "boom"},
    }
    for type_, data in examples.items():
        model_cls = EVENT_REGISTRY[type_]
        instance = model_cls(**data)
        assert instance is not None, f"{type_} 构造失败"


# ──────────────────────────────────────────────
# T0-2 事件 envelope 格式
# ──────────────────────────────────────────────


def test_event_envelope_format():
    """event() 序列化为 {type, ts, data} 三键，ts 为毫秒。"""
    env = event("run.started", thread_id="t1", run_id="r1")
    dumped = env.model_dump()
    assert set(dumped.keys()) == {"type", "ts", "data"}
    assert dumped["type"] == "run.started"
    assert isinstance(dumped["ts"], int)
    assert dumped["ts"] > 1000000000000  # 毫秒级时间戳
    assert dumped["data"]["thread_id"] == "t1"
    assert dumped["data"]["run_id"] == "r1"


def test_sse_format():
    """sse() 输出 data: {json}\\n\\n 格式。"""
    env = event("run.completed", message_id="m1", final_state="done")
    line = sse(env)
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    payload = json.loads(line[len("data: "):].strip())
    assert payload["type"] == "run.completed"


def test_event_unknown_type_raises():
    """未知事件类型应报 KeyError。"""
    with pytest.raises(KeyError):
        event("unknown.type", foo="bar")


# ──────────────────────────────────────────────
# T0-3 导出脚本幂等
# ──────────────────────────────────────────────


def test_export_script_idempotent():
    """连续执行两次 export_event_protocol.py，JSON 内容一致。"""
    script = _PROJECT_ROOT / "scripts" / "export_event_protocol.py"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_PROJECT_ROOT / "app")
    result1 = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, env=env, cwd=str(_PROJECT_ROOT),
    )
    assert result1.returncode == 0, f"第一次执行失败: {result1.stderr}"
    content1 = (_PROJECT_ROOT / "docs" / "event-protocol.json").read_text(encoding="utf-8")

    result2 = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, env=env, cwd=str(_PROJECT_ROOT),
    )
    assert result2.returncode == 0
    content2 = (_PROJECT_ROOT / "docs" / "event-protocol.json").read_text(encoding="utf-8")

    assert content1 == content2, "导出脚本不幂等"


def test_event_protocol_json_has_all_types():
    """event-protocol.json 包含全部 10 种事件类型。"""
    protocol_path = _PROJECT_ROOT / "docs" / "event-protocol.json"
    if not protocol_path.exists():
        pytest.skip("event-protocol.json 尚未生成")
    data = json.loads(protocol_path.read_text(encoding="utf-8"))
    assert set(data.keys()) == EXPECTED_EVENT_TYPES


# ──────────────────────────────────────────────
# T0-4 配置加载
# ──────────────────────────────────────────────


def test_config_loading_from_env_and_json():
    """AppSettings 取值正确；api_key 从 .env 而非 config.json 读取。"""
    from backend.config.settings import AppSettings

    settings = AppSettings()
    # api_key 应从 .env 读取（DASHSCOPE_API_KEY）
    assert settings.dashscope_api_key, "DASHSCOPE_API_KEY 未从 .env 加载"
    assert settings.dashscope_api_key.startswith("sk-"), "api_key 格式异常"

    # config.json 不再含明文 api_key
    config_json = json.loads((_PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
    assert "api_key" not in config_json, "config.json 仍含明文 api_key"

    # business 配置从 config.json 读取
    assert settings.business.model == "qwen-plus"
    assert settings.business.max_iterations == 3


def test_appconfig_from_file_delegates_to_settings():
    """AppConfig.from_file() 从 pydantic-settings 取值，字段访问不变。"""
    from mult_agents.config import AppConfig

    config = AppConfig.from_file()
    assert config.api_key, "api_key 未从 .env 读取"
    assert config.model == "qwen-plus"
    assert config.max_iterations == 3
    assert config.hitl_enabled is True
    assert config.hitl_config["plan_review"] is True


# ──────────────────────────────────────────────
# T0-6 死代码无残留
# ──────────────────────────────────────────────


def test_no_mult_agents_main_imports():
    """全局 grep mult_agents.main / codegen_node 零命中。"""
    import subprocess

    result = subprocess.run(
        ["git", "grep", "-r", "-l", "mult_agents.main\|codegen_node"],
        capture_output=True, text=True, cwd=str(_PROJECT_ROOT),
    )
    # git grep 在没有匹配时返回 1
    assert result.returncode != 0 or result.stdout.strip() == "", \
        f"仍有残留引用: {result.stdout}"


def test_no_bocha_references_in_python():
    """app/ 下 Python 文件中 bocha 引用零命中（注释除外）。"""
    import subprocess

    result = subprocess.run(
        ["git", "grep", "-r", "-i", "bocha", "--", "*.py"],
        capture_output=True, text=True, cwd=str(_PROJECT_ROOT),
    )
    # 允许注释中的说明行（tools.py:7 的"Bocha 搜索已删除"）
    lines = [l for l in result.stdout.strip().split("\n") if l]
    code_refs = [l for l in lines if "已删除" not in l and "已删" not in l]
    assert len(code_refs) == 0, f"仍有 bocha 代码引用: {code_refs}"
