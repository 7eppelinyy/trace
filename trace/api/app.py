"""FastAPI 应用工厂与路由挂载。"""

from __future__ import annotations

from contextlib import asynccontextmanager
import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from trace.api.routers import ask, auth, digest, events, market, preferences, research, status, watchlist
from trace.app import AppContext, create_app


def create_api_app(ctx: AppContext | None = None) -> FastAPI:
    """创建并装配 FastAPI 应用实例。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 如果未注入 ctx，初始化全局 AppContext
        if not hasattr(app.state, "ctx") or app.state.ctx is None:
            app.state.ctx = ctx or create_app()
        from trace.common.modes import TraceMode
        TraceMode.validate()
        yield
        if hasattr(app.state, "ctx") and app.state.ctx:
            if hasattr(app.state.ctx, "close"):
                app.state.ctx.close()
            elif hasattr(app.state.ctx, "db") and app.state.ctx.db:
                app.state.ctx.db.close()

    app = FastAPI(
        title="Trace — 美股 + A股重大事件智能雷达 API",
        description=(
            "提供重大事件流、产业传导路径图、全链路证据溯源、"
            "标的自选监控与基于本地证据库的智能归因问答服务。"
        ),
        version="1.0.0",
        lifespan=lifespan,
    )

    if ctx is not None:
        app.state.ctx = ctx

    # 跨域配置（限制允许源，杜绝全通凭据泄露风险）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in os.environ.get("TRACE_CORS_ORIGINS", "").split(",") if origin.strip()],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # 挂载根级别探针 (方便 Docker / K8s / 负载均衡器标准探测)
    app.include_router(status.router)

    # 挂载 API v1 路由
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(events.router, prefix="/api/v1")
    app.include_router(watchlist.router, prefix="/api/v1")
    app.include_router(market.router, prefix="/api/v1")
    app.include_router(ask.router, prefix="/api/v1")
    app.include_router(digest.router, prefix="/api/v1")
    app.include_router(preferences.router, prefix="/api/v1")
    app.include_router(research.router, prefix="/api/v1")
    app.include_router(status.router, prefix="/api/v1")

    @app.get("/", tags=["root"])
    def root():
        return {
            "name": "Trace API",
            "version": "1.0.0",
            "docs_url": "/docs",
            "redoc_url": "/redoc",
            "status": "online",
        }

    return app


# 供 uvicorn 直接加载的默认 app 实例
app = create_api_app()
