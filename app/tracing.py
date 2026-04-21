"""OpenTelemetry bootstrap (opt-in).

Tracing is enabled when either ``EGG_OTEL_ENDPOINT`` or
``OTEL_EXPORTER_OTLP_ENDPOINT`` is set. When the env vars are absent the
``configure_tracing()`` call is a pure no-op: the ``opentelemetry`` package
is a lazy optional dependency (install with ``pip install -e '.[otel]'``)
and we never import it if OTel is disabled.

Rationale: tracing is an operator concern, not part of the MVP contract.
Pinning hard OTel dependencies on every EGG install would balloon the
runtime image and force a collector endpoint even for dev smoke tests.

State shape:
  The tracing state (instrumented? enabled?) lives in a ``_TracingState``
  object, not at module scope. A ``reset_for_tests()`` helper restores
  the initial state so tests that install/uninstall OTel across cases
  do not need to ``sys.modules.pop("app.tracing")`` — they can call
  ``reset_for_tests()`` instead.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# Local stdlib logger: importing app.logging here would create a cycle
# (app.logging configure() pulls this module for the processor below).
logger = logging.getLogger("egg.tracing")

if TYPE_CHECKING:  # pragma: no cover - import-only typing aid
    from fastapi import FastAPI


@dataclass
class _TracingState:
    instrumented: bool = False
    enabled: bool = False


# Module-scoped holder. Still a single instance — but swapping the
# dataclass out via ``reset_for_tests()`` is cleaner than rebinding two
# booleans, and ``is_enabled()`` / the structlog processor read through
# the holder so they always see the current value.
_state = _TracingState()


def is_enabled() -> bool:
    """Return True when configure_tracing() has installed OTel on the app."""
    return _state.enabled


def reset_for_tests() -> None:
    """Revert the module to its uninstalled state.

    For tests only. Production code never needs to call this — the real
    instrumentation is installed exactly once at startup. Tests that
    toggle the OTel env var between cases should call this to drop the
    cached ``instrumented`` flag instead of reaching for
    ``sys.modules.pop``.
    """
    global _state
    _state = _TracingState()


def _otel_endpoint() -> str | None:
    endpoint = os.getenv("EGG_OTEL_ENDPOINT") or os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    endpoint = (endpoint or "").strip()
    return endpoint or None


def configure_tracing(app: FastAPI) -> bool:
    """Wire OpenTelemetry auto-instrumentation onto the FastAPI app.

    Returns True when tracing was actually installed, False when the
    opt-in env var was unset or the optional dependencies are not
    importable.
    """
    if _state.instrumented:
        return _state.enabled

    endpoint = _otel_endpoint()
    if endpoint is None:
        _state.instrumented = True
        logger.info("tracing_disabled: no EGG_OTEL_ENDPOINT / OTEL_EXPORTER_OTLP_ENDPOINT set")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        _state.instrumented = True
        logger.warning("tracing_disabled: opentelemetry not installed (%s)", exc)
        return False

    service_name = os.getenv("EGG_OTEL_SERVICE_NAME", "egg-api")
    env = os.getenv("EGG_ENV", "development")
    resource = Resource.create({"service.name": service_name, "deployment.environment": env})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    HTTPXClientInstrumentor().instrument()

    _state.instrumented = True
    _state.enabled = True
    logger.info("tracing_enabled endpoint=%s service_name=%s", endpoint, service_name)
    return True


def current_trace_and_span_ids() -> tuple[str | None, str | None]:
    """Return ``(trace_id, span_id)`` as hex strings, or ``(None, None)``.

    Safe to call whether OTel is installed or not — falls back to ``None``
    when the import fails or no span is active.
    """
    if not _state.enabled:
        return None, None
    try:
        from opentelemetry import trace
    except ImportError:
        return None, None
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if not ctx or not ctx.is_valid:
        return None, None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def structlog_tracing_processor(_logger: Any, _method: str, event_dict: dict) -> dict:
    """Structlog processor: attach trace_id/span_id to every event."""
    trace_id, span_id = current_trace_and_span_ids()
    if trace_id:
        event_dict.setdefault("trace_id", trace_id)
    if span_id:
        event_dict.setdefault("span_id", span_id)
    return event_dict
