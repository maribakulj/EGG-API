from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app import __version__
from app.admin_api.keys import router as admin_keys_router
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
    usage_persist_errors,
)
from app.public_api.routes import router as public_router
from app.runtime_paths import check_rate_limit_worker_safety, is_production
from app.tracing import configure_tracing

configure_logging()
logger = structlog.get_logger("egg.http")

# Multi-worker + in-memory rate limit is a silent security downgrade. Crash
# on prod, warn in dev. Runs at import so uvicorn workers pick it up before
# any request is served.
check_rate_limit_worker_safety()


async def _purge_loop() -> None:
    """Periodically evict expired admin sessions and stale usage events.

    Disabled when ``storage.purge_interval_seconds`` is non-positive. The
    task survives errors: it logs and keeps looping so a transient SQLite
    failure does not silently stop the retention clock.

    Updates ``container.last_purge_state`` in-place so operators can read
    the last tick via ``/admin/v1/storage/stats``. No lock is taken
    because the dict is only written from this single background task
    and read (snapshotted via ``dict()``) from request handlers; that
    is safe under the GIL for the atomic ops we do here.
    """
    storage_cfg = container.config_manager.config.storage
    interval = int(storage_cfg.purge_interval_seconds)
    if interval <= 0:
        logger.info("purge_loop_disabled", interval=interval)
        return

    while True:
        try:
            await asyncio.sleep(interval)
            retention_days = int(
                container.config_manager.config.storage.usage_events_retention_days
            )
            sessions = await run_in_threadpool(container.store.purge_expired_ui_sessions)
            events = await run_in_threadpool(
                container.store.purge_usage_events_older_than, retention_days
            )
            container.last_purge_state.update(
                {
                    "last_run_at": time.time(),
                    "sessions_purged": int(sessions),
                    "events_purged": int(events),
                }
            )
            logger.info(
                "purge_loop_tick",
                sessions_purged=sessions,
                events_purged=events,
                retention_days=retention_days,
            )
        except asyncio.CancelledError:
            logger.info("purge_loop_cancelled")
            raise
        except Exception:
            state = container.last_purge_state
            state["errors"] = int(state.get("errors", 0)) + 1
            logger.exception("purge_loop_tick_failed")


@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI):
    task = asyncio.create_task(_purge_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# Hide the interactive explorers in production: they list every route
# (including admin endpoints) and let anonymous callers fingerprint the
# surface. The custom /v1/openapi.json stays on in every environment for
# programmatic clients.
_PROD = is_production()
app = FastAPI(
    title="EGG-API",
    version=__version__,
    docs_url=None if _PROD else "/docs",
    redoc_url=None if _PROD else "/redoc",
    openapi_url=None if _PROD else "/openapi.json",
    lifespan=_lifespan,
)

# Rewrite request.client.host and request.url.scheme from X-Forwarded-* when
# the request arrives from a configured trusted hop. Without this, every
# client behind the reverse proxy shares the proxy's IP for rate-limiting
# and audit logs.
_proxy_cfg = container.config_manager.config.proxy
_trusted = _proxy_cfg.trusted_proxies
if _trusted:
    trusted_hosts = "*" if _trusted == ["*"] else ",".join(_trusted)
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted_hosts)

# Reject requests whose ``Host`` header does not match the configured list.
# Starlette compares case-insensitively and supports a single ``*`` suffix
# wildcard (``*.example.org``). Empty list → middleware skipped (local dev
# and tests run against 127.0.0.1/testserver where host validation would
# only create friction).
_allowed_hosts = [h for h in _proxy_cfg.allowed_hosts if h]
if _allowed_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)

# Expose the container on ``app.state`` so handlers/tests can take it via
# ``Depends(get_container)`` (see app.dependencies.get_container) instead of
# the module-level singleton. The singleton stays as a back-compat path —
# new code should prefer the Depends flavor to keep routes injectable.
app.state.container = container

# Wire OpenTelemetry traces/spans if configured. No-op when neither
# EGG_OTEL_ENDPOINT nor OTEL_EXPORTER_OTLP_ENDPOINT is set. Must run before
# the routers are attached so the instrumentation sees every endpoint.
configure_tracing(app)

app.include_router(public_router)
app.include_router(admin_router)
# /admin/v1/keys lives in its own module (Sprint 13) to keep the CRUD
# surface isolated from the setup/config routes.
app.include_router(admin_keys_router)
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
        # SQLite lookup is off-loaded to the threadpool so this middleware
        # never blocks the event loop (the rest of the app is FastAPI sync
        # routes which are already threadpool-scheduled).
        raw_key = request.headers.get("x-api-key")
        resolved_key_id: str | None = None
        if raw_key:
            try:
                identity = await run_in_threadpool(container.api_keys.get_identity, raw_key)
            except Exception:
                identity = None
                logger.exception("usage_audit_identity_lookup_failed")
            if identity is not None:
                resolved_key_id = identity.key_id
                # Propagate the public key_id into the structlog context so
                # anything logged for the remainder of the request carries
                # it (adapter calls, purge tick, handler exceptions…).
                structlog.contextvars.bind_contextvars(key_id=resolved_key_id)

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
            await run_in_threadpool(
                container.store.log_usage_event,
                request_id,
                endpoint,
                request.method,
                status_code,
                resolved_key_id,
                str(subject),
                latency_ms,
                error_code,
            )
        except Exception:
            # Storage failure must not hide the original response/exception.
            # Bump the counter so a sustained SQLite outage trips an alert
            # rather than rotting silently behind the `logger.exception`.
            logger.exception("usage_event_persist_failed", request_id=request_id)
            usage_persist_errors.inc()

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
