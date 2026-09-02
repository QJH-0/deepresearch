from functools import lru_cache

from backend.config import AppSettings
from .workflow_service import WorkflowService
from .research_service import ResearchService, get_research_service
from .task_registry import TaskRegistry, ConcurrentRunError, get_task_registry, init_task_registry


@lru_cache(maxsize=1)
def get_workflow_service() -> WorkflowService:
    settings = AppSettings()
    return WorkflowService(config_path=settings.config_path)


__all__ = [
    "WorkflowService",
    "get_workflow_service",
    "ResearchService",
    "get_research_service",
    "TaskRegistry",
    "ConcurrentRunError",
    "get_task_registry",
    "init_task_registry",
]
