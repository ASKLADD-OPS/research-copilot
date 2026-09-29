"""v1 路由聚合。"""

from fastapi import APIRouter

from app.api.v1 import chat, graph, papers, qa, tasks, tools, writing

api_router = APIRouter()
api_router.include_router(papers.router)
api_router.include_router(qa.router)
api_router.include_router(chat.router)
api_router.include_router(graph.router)
api_router.include_router(writing.router)
api_router.include_router(tools.router)
api_router.include_router(tasks.router)

__all__ = ["api_router"]
