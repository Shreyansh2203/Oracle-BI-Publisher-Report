from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from bip_api import __version__
from bip_api.cache import ReportCache
from bip_api.client import make_session
from bip_api.config import get_settings
from bip_api.models import HealthResponse
from bip_api.routers import reports

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    session = make_session(pool_size=settings.http_pool_size)
    app.state.http_session = session
    app.state.report_cache = ReportCache(settings.cache_ttl) if settings.cache_ttl > 0 else None

    log.info(
        "Started: pool_size=%d cache_ttl=%ds",
        settings.http_pool_size,
        settings.cache_ttl,
    )
    yield

    session.close()
    log.info("Stopped")


def create_app() -> FastAPI:
    settings = get_settings()

    application = FastAPI(
        title="BIP Downloader API",
        description="FastAPI service for downloading Oracle BI Publisher reports via SOAP",
        version=__version__,
        lifespan=lifespan,
        # Hide docs in production unless debug is on
        docs_url="/docs",
        redoc_url="/redoc",
    )

    origins = (
        ["*"]
        if settings.cors_origins.strip() == "*"
        else [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @application.middleware("http")
    async def log_requests(request: Request, call_next: object) -> Response:
        request_id = str(uuid.uuid4())[:8]
        start = time.perf_counter()
        response: Response = await call_next(request)  # type: ignore[operator]
        duration_ms = (time.perf_counter() - start) * 1000
        log.info(
            "%s %s %s %.1fms rid=%s",
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        response.headers["X-Request-Id"] = request_id
        return response

    application.include_router(reports.router)

    @application.get("/health", response_model=HealthResponse, tags=["health"])
    def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__)

    return application


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "bip_api.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )


if __name__ == "__main__":
    run()
