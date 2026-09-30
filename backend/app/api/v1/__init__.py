"""v1 路由聚合。"""

from fastapi import APIRouter

from app.api.v1 import chat, graph, health, papers, qa, tools, writing

api_router = APIRouter()
api_router.include_router(health.router)  # /health/db —— 三库状态
api_router.include_router(papers.router)
api_router.include_router(qa.router)
api_router.include_router(chat.router)
api_router.include_router(graph.router)
api_router.include_router(writing.router)
api_router.include_router(tools.router)

__all__ = ["api_router"]
