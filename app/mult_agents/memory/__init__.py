"""P5: 旧记忆系统已删除，替换为 langmem + PostgresStore（见 backend.service.memory_service）。

保留 base.py 中的 MemoryEntry / MemoryType 类型定义供向后兼容引用。
long_term.py / manager.py / short_term.py 已删除。
"""

# 仅保留类型定义，记忆逻辑已迁移至 backend.service.memory_service
from .base import MemoryEntry, MemoryType

__all__ = ["MemoryEntry", "MemoryType"]
