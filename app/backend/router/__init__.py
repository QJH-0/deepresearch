from .health_router import router as health_router
from .research_router import router as research_router
from .document_router import router as document_router
from .admin_router import router as admin_router

__all__ = ["health_router", "research_router", "document_router", "admin_router"]
