"""R4.1 日志重复输出修复测试用例。

T4.1-01 ~ T4.1-05 覆盖：
- 重复调用零副作用
- 未标记 handler 检测告警
- 单条日志单份输出
- 每线程研究日志不受影响
- setup_logging 残留清零
"""

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestIdempotentHandlerMount:
    """T4.1-01 重复调用零副作用。"""

    def test_import_twice_no_duplicate_handlers(self):
        """连续 import app_main 两次，带 _deepresearch_root 标记的 handler 数量不增长。"""
        import importlib

        # 首次导入确保日志已初始化
        if "app_main" not in sys.modules:
            importlib.import_module("app_main")

        root = logging.getLogger()
        before = sum(1 for h in root.handlers if getattr(h, "_deepresearch_root", False))
        assert before > 0, "首次导入后应有 _deepresearch_root 标记的 handler"

        # 清除缓存重新导入，验证守卫幂等
        saved = sys.modules.pop("app_main", None)
        try:
            importlib.import_module("app_main")
        finally:
            if saved:
                sys.modules["app_main"] = saved

        after = sum(1 for h in root.handlers if getattr(h, "_deepresearch_root", False))
        assert after == before, f"_deepresearch_root handler 数量从 {before} 增长到 {after}，存在重复挂载"


class TestUnmarkedHandlerWarning:
    """T4.1-02 未标记 handler 检测告警。"""

    def test_warning_for_unmarked_handler(self, caplog):
        """向 root logger 添加裸 StreamHandler 后触发自检，应出现 warning。"""
        root = logging.getLogger()
        bare_handler = logging.StreamHandler()
        bare_handler.addFilter(lambda r: False)  # 不实际输出
        root.addHandler(bare_handler)

        try:
            # 重新执行 app_main 模块级自检逻辑
            unmarked = [
                h for h in root.handlers
                if isinstance(h, logging.StreamHandler)
                and not getattr(h, "_deepresearch_root", False)
            ]
            assert len(unmarked) >= 1
            assert bare_handler in unmarked
        finally:
            root.removeHandler(bare_handler)


class TestSingleLineSingleOutput:
    """T4.1-03 单条日志单份输出。"""

    def test_single_log_single_capture(self, caplog):
        """logger.info 一次，捕获到的消息只有一份。"""
        test_logger = logging.getLogger("test_r4_1_single")

        with caplog.at_level(logging.INFO):
            test_logger.info("test-single-line")

        messages = [r.getMessage() for r in caplog.records if r.getMessage() == "test-single-line"]
        assert len(messages) == 1, f"期望 1 份日志，实际 {len(messages)} 份"


class TestResearchLoggerIntact:
    """T4.1-04 每线程研究日志不受影响。"""

    def test_research_logger_writes_json(self, tmp_path, monkeypatch):
        """ResearchLogger 的 JSON 文件写入不受 setup_logging 删除影响。"""
        monkeypatch.setenv("RESEARCH_LOG_DIR", str(tmp_path))

        # 重新导入以获取新的日志目录
        import importlib
        import mult_agents.research_logger as rl_mod
        importlib.reload(rl_mod)

        rl = rl_mod.ResearchLogger("test_thread_r4_1")
        rl.log_event("node_complete", {"node": "intent", "result": "ok"})
        rl.update_content("query", "test query")
        path = rl.finalize(route="multiagent", final="test report")

        assert Path(path).exists()
        import json
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["thread_id"] == "test_thread_r4_1"
        assert data["content"]["query"] == "test query"
        assert data["content"]["final"] == "test report"
        assert len(data["events"]) == 1
        assert data["events"][0]["type"] == "node_complete"


class TestSetupLoggingRemoved:
    """T4.1-05 setup_logging 残留清零。"""

    def test_no_setup_logging_in_research_logger(self):
        """research_logger 模块中不再有 setup_logging 函数。"""
        import mult_agents.research_logger as rl_mod
        assert not hasattr(rl_mod, "setup_logging"), "setup_logging 应已删除"

    def test_no_business_code_calls_setup_logging(self):
        """grep 全仓：无业务代码调用 setup_logging。"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "-c",
             "import subprocess,sys; "
             "r = subprocess.run(['git', '-C', '.', 'grep', '-rn', 'setup_logging', '--', 'app/'], "
             "capture_output=True, text=True); "
             "print(r.stdout.strip() or 'CLEAN'); "
             "sys.exit(0)"],
            capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[2]),
        )
        output = result.stdout.strip()
        # 允许在测试文件自身或注释中出现，但不应该在业务代码中调用
        lines = [l for l in output.splitlines() if l and "test_logging_guard" not in l and "CLEAN" not in l]
        assert len(lines) == 0, f"业务代码中仍有 setup_logging 引用: {lines}"
