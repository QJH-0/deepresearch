"""事件协议契约对拍：后端 schema ↔ 前端 TS 类型。

链路是 backend/schemas/events.py → docs/event-protocol.json → events.gen.ts。
其中只有前半段有生成器，`.ts` 是照着 json 手工维护的 —— 后端改了字段而前端
没跟上，编译期与运行期都不会报错，只会在线上表现为「某个字段是 undefined」。

本文件把这条链路钉死：协议文件过期、事件类型增删、字段名或必填性不一致，
都会在这里失败。

运行方式:
    cd D:\\Code\\LLMdev\\deepresearch
    set PYTHONPATH=app
    python -m pytest app/test/test_event_protocol_contract.py -v
"""

import json
import re
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT / "app"))

from backend.schemas.events import EVENT_REGISTRY  # noqa: E402

_PROTOCOL_JSON = _PROJECT_ROOT / "docs" / "event-protocol.json"
_FRONTEND_TYPES = _PROJECT_ROOT / "agent_front" / "src" / "types" / "events.gen.ts"

_INTERFACE_PATTERN = re.compile(r"export interface (\w+)\s*\{(.*?)\n\}", re.DOTALL)
_EVENT_TYPE_UNION_PATTERN = re.compile(r"export type EventType\s*=(.*?)\n\n", re.DOTALL)
_UNION_MEMBER_PATTERN = re.compile(r"'([^']+)'")
_FIELD_PATTERN = re.compile(r"^[ \t]*(\w+)(\?)?[ \t]*:", re.MULTILINE)


def _read_frontend_types() -> str:
    return _FRONTEND_TYPES.read_text(encoding="utf-8")


def _parse_interfaces(source: str) -> dict[str, dict[str, bool]]:
    """解析 export interface，返回 {接口名: {字段名: 是否可选}}。"""
    result: dict[str, dict[str, bool]] = {}
    for name, body in _INTERFACE_PATTERN.findall(source):
        fields: dict[str, bool] = {}
        for field, optional in _FIELD_PATTERN.findall(body):
            fields[field] = optional == "?"
        result[name] = fields
    return result


class TestProtocolJsonIsUpToDate:
    """docs/event-protocol.json 必须是后端 schema 的忠实导出。"""

    def test_protocol_file_matches_registry(self):
        expected = {name: model.model_json_schema() for name, model in EVENT_REGISTRY.items()}
        actual = json.loads(_PROTOCOL_JSON.read_text(encoding="utf-8"))

        assert actual == expected, (
            "docs/event-protocol.json 与 backend/schemas/events.py 不一致，"
            "请运行：python -m scripts.export_event_protocol"
        )


class TestFrontendEventTypeUnion:
    """前端 EventType 联合类型必须与后端事件名一一对应。"""

    def test_union_matches_registry(self):
        source = _read_frontend_types()
        match = _EVENT_TYPE_UNION_PATTERN.search(source)
        assert match, "未能在 events.gen.ts 中定位 EventType 联合类型"

        frontend_types = set(_UNION_MEMBER_PATTERN.findall(match.group(1)))
        backend_types = set(EVENT_REGISTRY)

        missing = backend_types - frontend_types
        extra = frontend_types - backend_types
        assert not missing and not extra, (
            f"事件类型不一致。前端缺少：{sorted(missing)}；前端多出：{sorted(extra)}"
        )


class TestFrontendDataInterfaces:
    """每个事件 data 接口的字段名与必填性必须与后端模型一致。"""

    def test_event_data_interfaces_match_models(self):
        interfaces = _parse_interfaces(_read_frontend_types())
        problems: list[str] = []

        for event_type, model in EVENT_REGISTRY.items():
            interface_name = model.__name__
            if interface_name not in interfaces:
                problems.append(f"{event_type}: 前端缺少接口 {interface_name}")
                continue

            frontend_fields = interfaces[interface_name]
            backend_fields = {
                name: not field.is_required() for name, field in model.model_fields.items()
            }

            if frontend_fields != backend_fields:
                problems.append(
                    f"{event_type} ({interface_name}): "
                    f"前端={frontend_fields} 后端={backend_fields}"
                )

        assert not problems, "事件 data 接口与后端模型不一致：\n" + "\n".join(problems)

    def test_nested_models_exported_to_frontend_stay_in_sync(self):
        """嵌套模型（如 SourceItem）同样会被前端引用，一并比对。"""
        interfaces = _parse_interfaces(_read_frontend_types())
        problems: list[str] = []

        for model in EVENT_REGISTRY.values():
            for name, field in model.model_fields.items():
                nested = getattr(field.annotation, "__name__", None)
                if nested is None or nested not in interfaces:
                    continue
                nested_model = field.annotation
                backend_fields = {
                    key: not value.is_required()
                    for key, value in nested_model.model_fields.items()
                }
                if interfaces[nested] != backend_fields:
                    problems.append(
                        f"{nested}: 前端={interfaces[nested]} 后端={backend_fields}"
                    )

        assert not problems, "嵌套模型与前端接口不一致：\n" + "\n".join(problems)
