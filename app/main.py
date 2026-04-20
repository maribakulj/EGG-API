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
logger = structlog.get_logger("egg.http")

# Hide the interactive explorers in production: they list every route
# (including admin endpoints) and let anonymous callers fingerprint the
# surface. The custom /v1/openapi.json stays on in every environment for
# programmatic clients.
_PROD = is_production()
app = FastAPI(
    title="EGG-API",
    version="0.1.0",
    docs_url=None if _PROD else "/docs",
    redoc_url=None if _PROD else "/redoc",
    openapi_url=None if _PROD else "/openapi.json",
)
app.include_router(public_router)
app.include_router(admin_router)
app.include_router(admin_ui_router)
app.mount(
    "/admin-static",
    StaticFiles(directory=Path(__file__).parent / "admin_ui" / "static"),
    name="admin-static",
)


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


def _route_template(request: Request, fallback: str) -> str:
    """Return the path template of the matched route (e.g. ``/v1/records/{id}``).

    Using ``request.url.path`` as a Prometheus label value would explode the
    time-series cardinality whenever a path parameter is present.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None) if route is not None else None
    return path or fallback


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

    response = None
    status_code = 500
    error_code: str | None = "unhandled_exception"
    try:
        response = await call_next(request)
        status_code = response.status_code
        error_code = None
        return response
    finally:
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
        raw_path = request.url.path
        endpoint = _route_template(request, fallback=raw_path)
        status_label = str(status_code)

        # Prometheus counters & histogram — labels use the route template to
        # keep cardinality bounded.
        request_count.labels(endpoint=endpoint, method=request.method, status=status_label).inc()
        request_duration.labels(endpoint=endpoint, method=request.method).observe(duration_s)
        if status_code == 429:
            scope = "public" if endpoint.startswith("/v1") else "admin"
            rate_limit_hits.labels(scope=scope).inc()

        logger.info(
            "request",
            request_id=request_id,
            method=request.method,
            path=raw_path,
            route=endpoint,
            status_code=status_code,
            latency_ms=latency_ms,
            key_id=resolved_key_id,
            error_code=error_code,
        )

        try:
            container.store.log_usage_event(
                request_id=request_id,
                endpoint=endpoint,
                method=request.method,
                status_code=status_code,
                api_key_id=resolved_key_id,
                subject=str(subject),
                latency_ms=latency_ms,
                error_code=error_code,
            )
        except Exception:
            # Storage failure must not hide the original response/exception.
            logger.exception("usage_event_persist_failed", request_id=request_id)

        structlog.contextvars.clear_contextvars()


@app.get("/metrics")
def metrics(request: Request) -> Response:
    """Expose Prometheus metrics in text exposition format.

    Protected: the caller must present either the admin API key (via
    ``X-API-Key``) or a bearer token equal to ``EGG_METRICS_TOKEN``. In
    development both checks are skipped when ``EGG_METRICS_TOKEN`` is unset
    and no admin key is provided, to preserve the local developer ergonomics.
    Set the env var in production to require authenticated scraping.
    """
    import os

    expected_token = os.getenv("EGG_METRICS_TOKEN", "").strip()
    auth_header = request.headers.get("authorization", "")
    bearer = ""
    if auth_header.lower().startswith("bearer "):
        bearer = auth_header.split(" ", 1)[1].strip()
    x_api_key = request.headers.get("x-api-key")

    if expected_token and bearer != expected_token and not container.api_keys.validate(x_api_key):
        raise AppError(
            "invalid_api_key",
            "/metrics requires a bearer token or admin API key",
            status_code=401,
        )
    # Refuse unauthenticated scraping in prod when no token is configured.
    if not expected_token and is_production() and not container.api_keys.validate(x_api_key):
        raise AppError(
            "invalid_api_key",
            "/metrics is not exposed without EGG_METRICS_TOKEN in production",
            status_code=401,
        )

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
