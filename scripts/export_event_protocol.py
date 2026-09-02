"""生成 docs/event-protocol.json：type -> JSON Schema，供前端生成 TS 类型。

用法:
    cd D:\\Code\\LLMdev\\deepresearch
    python -m scripts.export_event_protocol
    或
    set PYTHONPATH=app && python scripts/export_event_protocol.py
"""

import json
import sys
from pathlib import Path

# 确保能 import app.backend.schemas.events
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT_ROOT / "app"))

from backend.schemas.events import EVENT_REGISTRY  # noqa: E402


def main():
    out = {t: m.model_json_schema() for t, m in EVENT_REGISTRY.items()}
    path = _PROJECT_ROOT / "docs" / "event-protocol.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written: {path} ({len(out)} event types)")


if __name__ == "__main__":
    main()
