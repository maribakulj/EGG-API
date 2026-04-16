from __future__ import annotations

import time
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.admin_api.routes import router as admin_router
from app.admin_ui.routes import router as admin_ui_router
from app.dependencies import container
from app.errors import AppError, to_error_response
from app.logging import configure as configure_logging
from app.logging.request_context import get_request_id
from app.metrics import (
    rate_limit_hits,
    render_latest,
    request_count,
    request_duration,
)
from app.public_api.routes import router as public_router
from app.runtime_paths import is_production

configure_logging()
logger = structlog.get_logger("pisco.http")

app = FastAPI(title="PISCO-API", version="0.1.0")
app.include_router(public_router)
app.include_router(admin_router)
app.include_router(admin_ui_router)
app.mount("/admin-static", StaticFiles(directory=Path(__file__).parent / "admin_ui" / "static"), name="admin-static")


def _configure_cors(app_instance: FastAPI) -> None:
    cors_cfg = container.config_manager.config.cors
    mode = (cors_cfg.mode or "off").lower()
    if mode == "off":
        return
    if mode == "wide_open":
        origins: list[str] = ["*"]
    else:  # allowlist
        origins = [o for o in cors_cfg.allow_origins if o]
        if not origins:
            return
    app_instance.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=cors_cfg.allow_methods or ["GET"],
        allow_headers=cors_cfg.allow_headers or ["x-api-key", "content-type"],
        max_age=600,
    )


_configure_cors(app)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    # Baseline hardening for every response.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    # Admin UI is same-origin only; block framing and cross-origin embedding.
    if request.url.path.startswith("/admin"):
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; form-action 'self'; "
            "frame-ancestors 'none'; base-uri 'self'",
        )
    if is_production():
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


@app.middleware("http")
async def usage_audit_middleware(request: Request, call_next):
    started = time.monotonic()
    request_id = get_request_id(request)
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )
    response = await call_next(request)

    # Never log the raw API key. Resolve it to the key_id (public label);
    # fall back to the client IP for anonymous or invalid-key traffic.
    raw_key = request.headers.get("x-api-key")
    resolved_key_id: str | None = None
    if raw_key:
        identity = container.api_keys.get_identity(raw_key)
        if identity is not None:
            resolved_key_id = identity.key_id

    client_host = request.client.host if request.client else "anonymous"
    subject = resolved_key_id or client_host
    duration_s = time.monotonic() - started
    latency_ms = int(duration_s * 1000)
    endpoint = request.url.path
    status = str(response.status_code)

    # Prometheus counters & histogram.
    request_count.labels(endpoint=endpoint, method=request.method, status=status).inc()
    request_duration.labels(endpoint=endpoint, method=request.method).observe(duration_s)
    if response.status_code == 429:
        scope = "public" if endpoint.startswith("/v1") else "admin"
        rate_limit_hits.labels(scope=scope).inc()

    logger.info(
        "request",
        request_id=request_id,
        method=request.method,
        path=endpoint,
        status_code=response.status_code,
        latency_ms=latency_ms,
        key_id=resolved_key_id,
    )

    container.store.log_usage_event(
        request_id=request_id,
        endpoint=endpoint,
        method=request.method,
        status_code=response.status_code,
        api_key_id=resolved_key_id,
        subject=str(subject),
        latency_ms=latency_ms,
        error_code=None,
    )
    structlog.contextvars.clear_contextvars()
    return response


@app.get("/metrics")
def metrics() -> Response:
    """Expose Prometheus metrics in text exposition format."""
    body, content_type = render_latest()
    return Response(content=body, media_type=content_type)


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return to_error_response(request, exc)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_parameter",
                "message": "Validation error",
                "details": {"errors": exc.errors()},
                "request_id": request.headers.get("x-request-id", "generated"),
            }
        },
    )


@app.get("/v1/openapi.json")
def openapi_json() -> dict[str, object]:
    return app.openapi()
