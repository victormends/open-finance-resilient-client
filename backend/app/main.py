"""FastAPI entry point wiring lifespan-managed httpx.AsyncClient, X-Request-ID middleware, and routes."""

from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.app.client import (
    DEFAULT_TIMEOUT,
    JitterFn,
    SleepFn,
    set_client,
)
from backend.app.routes import client_router, get_request_id, sandbox_router
from backend.app.sandbox import SandboxAPIError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize app-scoped shared async HTTP client on startup and close it on shutdown."""
    factory = getattr(app.state, "client_factory", None)
    client: Optional[httpx.AsyncClient] = factory() if factory is not None else None
    app.state.http_client = client
    if client is not None:
        set_client(client)
    app.state.ready = True
    try:
        yield
    finally:
        app.state.ready = False
        app.state.http_client = None
        if client is not None and not client.is_closed:
            await client.aclose()


def create_app(
    base_url: Optional[str] = None,
    retry_base: Optional[float] = None,
    retry_factor: Optional[float] = None,
    max_retries: Optional[int] = None,
    sleep_fn: Optional[SleepFn] = None,
    respect_retry_after: bool = True,
    jitter_fn: Optional[JitterFn] = None,
) -> FastAPI:
    """Construct and configure the FastAPI application."""
    app = FastAPI(
        title="Open Finance Resilient Client & Sandbox",
        version="0.1.0",
        description=(
            "Reference implementation of an async Open Finance client with selective retry, "
            "request_id observability, and a deterministic HTTP 428 MFA state machine."
        ),
        lifespan=lifespan,
    )

    resolved_base_url = base_url or os.getenv("OPEN_FINANCE_BASE_URL", "inprocess://sandbox")
    resolved_timeout = float(os.getenv("OPEN_FINANCE_TIMEOUT", str(DEFAULT_TIMEOUT)))

    app.state.retry_base = (
        retry_base
        if retry_base is not None
        else float(os.getenv("OPEN_FINANCE_RETRY_BASE", "0.5"))
    )
    app.state.retry_factor = (
        retry_factor
        if retry_factor is not None
        else float(os.getenv("OPEN_FINANCE_RETRY_FACTOR", "2.0"))
    )
    app.state.max_retries = (
        max_retries
        if max_retries is not None
        else int(os.getenv("OPEN_FINANCE_MAX_RETRIES", "4"))
    )
    app.state.sleep_fn = sleep_fn
    app.state.respect_retry_after = respect_retry_after
    app.state.jitter_fn = jitter_fn
    app.state.ready = False
    app.state.http_client = None

    if resolved_base_url == "inprocess://sandbox":
        app.state.client_factory = lambda: httpx.AsyncClient(
            base_url="http://sandbox.local",
            transport=httpx.ASGITransport(app=app),
            timeout=resolved_timeout,
        )
    else:
        app.state.client_factory = lambda: httpx.AsyncClient(
            base_url=resolved_base_url,
            timeout=resolved_timeout,
        )

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Any:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(SandboxAPIError)
    async def sandbox_error_handler(request: Request, exc: SandboxAPIError) -> JSONResponse:
        request_id = get_request_id(request)
        payload: Dict[str, Any] = {
            "code": exc.code,
            "message": exc.message,
            "request_id": request_id,
            **exc.extra,
        }
        return JSONResponse(
            status_code=exc.status_code,
            content=payload,
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        request_id = get_request_id(request)
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "Request payload or query validation failed.",
                "request_id": request_id,
                "detail": jsonable_encoder(exc.errors()),
            },
            headers={"X-Request-ID": request_id},
        )

    @app.get("/health", tags=["observability"])
    async def health() -> Dict[str, str]:
        return {"status": "ok", "service": "open-finance-resilient-client"}

    @app.get("/ready", tags=["observability"])
    async def ready() -> JSONResponse:
        if not getattr(app.state, "ready", False):
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "http_client": "closed"},
            )
        client = getattr(app.state, "http_client", None)
        if client is None or client.is_closed:
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "http_client": "closed"},
            )
        return JSONResponse(
            status_code=200,
            content={"status": "ready", "http_client": "open", "sandbox": "active"},
        )

    app.include_router(sandbox_router)
    app.include_router(client_router)
    return app


app = create_app()
