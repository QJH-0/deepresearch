"""结构化研究日志模块。

参考 gpt-researcher/gpt_researcher/utils/logging_config.py 的 JSONResearchHandler，
为每个研究任务生成带时间戳的 JSON 日志文件，记录：
- 查询内容
- 每个节点的输入/输出
- 检索统计
- 证据池状态
- 最终报告
- 执行时间
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("mult_agents.research_logger")

# 日志输出目录 — 基于项目根目录（app/ 的上级）解析为绝对路径
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOGS_DIR = Path(os.getenv("RESEARCH_LOG_DIR", str(_PROJECT_ROOT / "output" / "logs" / "research")))
if not _LOGS_DIR.is_absolute():
    _LOGS_DIR = _PROJECT_ROOT / _LOGS_DIR
_LOGS_DIR.mkdir(parents=True, exist_ok=True)


class ResearchLogger:
    """单次研究任务的结构化日志记录器。

    参考: gpt-researcher/gpt_researcher/utils/logging_config.py
    每个 thread_id 对应一个 ResearchLogger 实例，
    全过程记录到 {thread_id}_{timestamp}.json 文件。
    """

    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        self.timestamp = datetime.now().isoformat()
        self.events: list[dict[str, Any]] = []
        self.content: dict[str, Any] = {
            "thread_id": thread_id,
            "query": "",
            "intent": "",
            "plan": "",
            "sources": [],
            "evidence_pool": [],
            "web_stats": {},
            "local_stats": {},
            "final": "",
            "route": "",
            "node_count": 0,
            "elapsed_seconds": 0.0,
        }
        self._start_time = datetime.now()
        self._json_path = _LOGS_DIR / f"{thread_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        self._log_path = _LOGS_DIR / f"{thread_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    def log_event(self, event_type: str, data: dict[str, Any]) -> None:
        """记录一个事件。"""
        event = {
            "timestamp": datetime.now().isoformat(),
            "type": event_type,
            "data": data,
        }
        self.events.append(event)
        # 同时写入传统日志
        logger.info("[%s] %s | %s", self.thread_id, event_type, json.dumps(data, ensure_ascii=False)[:200])
        self._save()

    def update_content(self, key: str, value: Any) -> None:
        """更新研究内容摘要。"""
        self.content[key] = value
        self._save()

    def _save(self) -> None:
        """将完整日志写入 JSON 文件。"""
        data = {
            "thread_id": self.thread_id,
            "timestamp": self.timestamp,
            "events": self.events,
            "content": self.content,
            "elapsed_seconds": (datetime.now() - self._start_time).total_seconds(),
        }
        try:
            self._json_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("写入研究日志失败: %s", e)

    def finalize(self, route: str = "", final: str = "") -> str:
        """研究完成时调用，写入最终结果并返回 JSON 文件路径。"""
        self.content["route"] = route
        if final:
            self.content["final"] = final[:5000]  # 限制长度避免日志过大
        self.content["elapsed_seconds"] = (datetime.now() - self._start_time).total_seconds()
        self.content["node_count"] = len([e for e in self.events if e["type"] == "node_complete"])
        self._save()
        logger.info("[%s] 研究日志已保存 | path=%s | events=%d | elapsed=%.2fs",
                     self.thread_id, self._json_path, len(self.events),
                     self.content["elapsed_seconds"])
        return str(self._json_path)


# ── 全局 logger 实例缓存（按 thread_id 隔离）──

_active_loggers: dict[str, ResearchLogger] = {}


def get_research_logger(thread_id: str) -> ResearchLogger:
    """获取或创建某个会话的研究日志记录器。

    参考 gpt-researcher 的 get_research_logger() 函数。
    """
    if thread_id not in _active_loggers:
        _active_loggers[thread_id] = ResearchLogger(thread_id)
    return _active_loggers[thread_id]


def close_research_logger(thread_id: str, route: str = "", final: str = "") -> str:
    """关闭并保存某个会话的研究日志。"""
    logger_instance = _active_loggers.pop(thread_id, None)
    if logger_instance is None:
        return ""
    return logger_instance.finalize(route=route, final=final)


def setup_logging() -> None:
    """初始化全局日志配置。

    参考: gpt-researcher/gpt_researcher/utils/logging_config.py 的 setup_research_logging()
    """
    # 确保日志目录存在
    _LOGS_DIR.mkdir(parents=True, exist_ok=True)

    # 配置 root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # 控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    # 避免重复添加
    has_console = any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers)
    if not has_console:
        root_logger.addHandler(console_handler)

    # 文件 handler（通用日志）
    log_file = _LOGS_DIR / f"app_{datetime.now().strftime('%Y%m%d')}.log"
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    root_logger.addHandler(file_handler)

    # 降低第三方库的日志级别
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
