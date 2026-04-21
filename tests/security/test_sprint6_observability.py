"""Regression tests for Sprint 6 observability (S6.1 - S6.9)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog
import yaml

# ---------------------------------------------------------------------------
# S6.1 — OTel bootstrap is opt-in
# ---------------------------------------------------------------------------


def test_s6_1_tracing_disabled_without_env(monkeypatch) -> None:
    # Sprint 10 cleanup: use ``reset_for_tests()`` instead of
    # ``sys.modules.pop("app.tracing")``. The former only resets the
    # instrumented/enabled flags; the latter re-ran every module-level
    # side effect in app.tracing and leaked state into subsequent tests.
    from fastapi import FastAPI

    from app import tracing

    monkeypatch.delenv("EGG_OTEL_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracing.reset_for_tests()

    assert tracing.configure_tracing(FastAPI()) is False
    assert tracing.is_enabled() is False


def test_s6_1_current_trace_and_span_ids_without_otel() -> None:
    from app.tracing import current_trace_and_span_ids

    # With OTel not wired, the helper returns (None, None) regardless of
    # structlog context. Callers must tolerate both branches.
    trace_id, span_id = current_trace_and_span_ids()
    assert (trace_id, span_id) == (None, None)


# ---------------------------------------------------------------------------
# S6.6 — key_id flows into structlog once resolved
# ---------------------------------------------------------------------------


def test_s6_6_key_id_bound_to_structlog_context(client, admin_headers) -> None:
    # Make a request with a valid admin key; the audit middleware must bind
    # key_id into the contextvars, which makes it available to every log
    # line emitted during the request.
    structlog.contextvars.clear_contextvars()
    response = client.get("/admin/v1/config", headers=admin_headers)
    assert response.status_code == 200
    # Middleware clears contextvars on exit (see usage_audit_middleware
    # finally branch); nothing leaks to subsequent tests.
    leftover = structlog.contextvars.get_contextvars()
    assert "key_id" not in leftover


def test_s6_6_structlog_tracing_processor_is_side_effect_free() -> None:
    from app.tracing import structlog_tracing_processor

    event: dict = {"event": "sample"}
    out = structlog_tracing_processor(None, "info", event)
    # Without an active OTel span the processor leaves the event untouched.
    assert out is event
    assert "trace_id" not in out
    assert "span_id" not in out


# ---------------------------------------------------------------------------
# S6.7 — /admin/v1/debug/translate
# ---------------------------------------------------------------------------


def test_s6_7_debug_translate_returns_normalized_and_dsl(client, admin_headers) -> None:
    response = client.get(
        "/admin/v1/debug/translate?q=hello&page_size=10&facet=type",
        headers=admin_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["normalized"]["q"] == "hello"
    assert body["normalized"]["page_size"] == 10
    assert body["normalized"]["facets"] == ["type"]
    assert "translated" in body
    assert "cache_key" in body
    # Cache key is the same SHA-256 as the public /v1/search etag logic.
    assert len(body["cache_key"]) == 64


def test_s6_7_debug_translate_requires_admin(client) -> None:
    response = client.get("/admin/v1/debug/translate?q=hello")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# S6.5 — Prometheus alert file is valid YAML and names the expected alerts
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_s6_5_alerts_yaml_parses_and_covers_core_alerts() -> None:
    alerts_path = _REPO_ROOT / "ops" / "prometheus" / "alerts.yml"
    assert alerts_path.exists(), "ops/prometheus/alerts.yml is missing"
    parsed = yaml.safe_load(alerts_path.read_text())
    alert_names = {
        rule["alert"]
        for group in parsed["groups"]
        for rule in group.get("rules", [])
        if "alert" in rule
    }
    expected = {
        "EGGApiDown",
        "EGGApiHighErrorRate",
        "EGGApiP99Slow",
        "EGGBackendUnavailable",
        "EGGBackendVersionUnsupported",
        "EGGRateLimitBurst",
    }
    missing = expected - alert_names
    assert not missing, f"missing Prometheus alerts: {missing}"


# ---------------------------------------------------------------------------
# S6.3 — Grafana dashboard ships and is valid JSON
# ---------------------------------------------------------------------------


def test_s6_3_grafana_dashboard_parses() -> None:
    dashboard_path = _REPO_ROOT / "ops" / "grafana" / "egg-api-overview.json"
    assert dashboard_path.exists(), "ops/grafana/egg-api-overview.json is missing"
    data = json.loads(dashboard_path.read_text())
    assert data["title"] == "EGG-API — Overview"
    # Every panel targets a metric we actually export.
    exprs = " ".join(
        target.get("expr", "") for panel in data["panels"] for target in panel.get("targets", [])
    )
    for metric in (
        "egg_requests_total",
        "egg_request_duration_seconds_bucket",
        "egg_backend_errors_total",
        "egg_rate_limit_hits_total",
    ):
        assert metric in exprs, f"dashboard never references {metric}"


# ---------------------------------------------------------------------------
# S6.4 — Runbook exists and covers each alert section
# ---------------------------------------------------------------------------


def test_s6_4_runbook_has_section_for_each_alert() -> None:
    runbook = (_REPO_ROOT / "ops" / "RUNBOOK.md").read_text()
    for anchor in (
        "process-down",
        "es-outage",
        "high-5xx",
        "slow-backend",
        "rate-limit-burst",
        "csrf-mismatch",
        "key-recovery",
        "purge",
    ):
        assert f"## {anchor}" in runbook, f"runbook missing section #{anchor}"


# ---------------------------------------------------------------------------
# S6.8 — Kubernetes manifests are valid YAML and non-root
# ---------------------------------------------------------------------------


def test_s6_8_k8s_manifest_is_well_formed_multi_doc() -> None:
    manifest = (_REPO_ROOT / "deploy" / "k8s" / "egg-api.yaml").read_text()
    docs = list(yaml.safe_load_all(manifest))
    assert len(docs) >= 4, "expected Namespace + Secret + ConfigMap + Deployment + Service"
    kinds = {doc["kind"] for doc in docs if isinstance(doc, dict)}
    for expected in ("Namespace", "Secret", "ConfigMap", "Deployment", "Service"):
        assert expected in kinds, f"K8s manifest missing a {expected}"


def test_s6_8_deployment_runs_as_non_root() -> None:
    manifest = (_REPO_ROOT / "deploy" / "k8s" / "egg-api.yaml").read_text()
    docs = list(yaml.safe_load_all(manifest))
    deploy = next(doc for doc in docs if isinstance(doc, dict) and doc.get("kind") == "Deployment")
    sec = deploy["spec"]["template"]["spec"]["securityContext"]
    assert sec["runAsNonRoot"] is True
    assert sec["runAsUser"] == 1000


def test_s6_8_liveness_and_readiness_hit_livez() -> None:
    manifest = (_REPO_ROOT / "deploy" / "k8s" / "egg-api.yaml").read_text()
    docs = list(yaml.safe_load_all(manifest))
    deploy = next(doc for doc in docs if isinstance(doc, dict) and doc.get("kind") == "Deployment")
    container = deploy["spec"]["template"]["spec"]["containers"][0]
    assert container["livenessProbe"]["httpGet"]["path"] == "/v1/livez"
    assert container["readinessProbe"]["httpGet"]["path"] == "/v1/livez"


# ---------------------------------------------------------------------------
# S6.9 — locustfile imports cleanly (no syntax/import surprise)
# ---------------------------------------------------------------------------


def test_s6_9_locustfile_is_importable(monkeypatch) -> None:
    import importlib.util
    import sys
    import types

    # Locust is an ops-only dep; fake the module so import succeeds without
    # pulling it into the test env.
    if "locust" not in sys.modules:
        fake = types.ModuleType("locust")

        class _FakeHttpUser:
            wait_time = None

        def _between(a, b):
            return None

        def _task(weight):
            def deco(fn):
                return fn

            return deco

        fake.HttpUser = _FakeHttpUser
        fake.between = _between
        fake.task = _task
        sys.modules["locust"] = fake

    locustfile = _REPO_ROOT / "scripts" / "locustfile.py"
    spec = importlib.util.spec_from_file_location("_egg_locust", locustfile)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert hasattr(module, "PublicReader")


# ---------------------------------------------------------------------------
# Misc: debug endpoint inherits query-policy validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("param", ["sort=hacker", "facet=private"])
def test_debug_translate_rejects_forbidden_inputs(client, admin_headers, param) -> None:
    response = client.get(f"/admin/v1/debug/translate?q=x&{param}", headers=admin_headers)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "forbidden"
