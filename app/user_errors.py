"""User-facing error translation (Sprint 16).

The config models and the query-policy engine throw machine-readable
codes (``invalid_parameter``, ``backend_unavailable``, …). Those are
perfect for integrators but unreadable for a librarian running
``egg-api check-config`` for the first time.  This module keeps a
small dictionary of hints keyed by (code, field-or-topic) and falls
back to a generic "contact your administrator" message when nothing
matches.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from app.errors import AppError

# (error_code, field/topic) → (plain-language message, suggestion).  The
# second key is matched against ``details`` keys in order; use ``None``
# as a wildcard fallback for the code alone.
_HINTS: dict[tuple[str, str | None], tuple[str, str]] = {
    ("invalid_parameter", "field"): (
        "A value in your configuration was rejected.",
        "Re-open the wizard or edit config/egg.yaml and fix the field named in the details.",
    ),
    ("invalid_parameter", "key_id"): (
        "The key label is not valid.",
        "Labels must be 1-64 characters, letters/digits/dot/underscore/dash only.",
    ),
    ("invalid_parameter", "parameter"): (
        "A public query parameter is missing or malformed.",
        "Check the parameter mentioned in details and try again.",
    ),
    ("backend_unavailable", None): (
        "EGG-API could not reach the search backend.",
        "Verify that Elasticsearch/OpenSearch is running and that the URL + credentials are correct (wizard step 1).",
    ),
    ("unsupported_backend_version", None): (
        "The backend version is too old for EGG-API.",
        "Elasticsearch 7 or later, or OpenSearch 1 or later, is required.",
    ),
    ("forbidden", None): (
        "The caller does not have permission for this action.",
        "Sign in with an active admin key (see wizard step 6).",
    ),
    ("not_found", "key_id"): (
        "No API key exists with that label.",
        "List keys in the admin UI or with `GET /admin/v1/keys`.",
    ),
    ("not_found", None): (
        "The resource you requested does not exist.",
        "Double-check the path (record id, key label…) and try again.",
    ),
    ("conflict", "key_id"): (
        "An API key already exists with that label.",
        "Pick a different label, or rotate the existing key instead.",
    ),
    ("configuration_error", None): (
        "The current configuration is incomplete or inconsistent.",
        "Run `egg-api check-config` (CLI) or visit the setup wizard to review it.",
    ),
    ("invalid_api_key", None): (
        "The admin key you provided is not valid.",
        "Copy the key printed by `egg-api start` (or stored in data/bootstrap_admin.key).",
    ),
}


def translate_app_error(exc: AppError) -> dict[str, Any]:
    """Return a ``{code, message, user_message, suggestion, details}`` dict.

    Picks the most specific (code, detail-key) hint available. Keeps
    the original machine-readable code so logs and integrators still
    see the same payload.
    """
    details = dict(exc.details or {})
    hint: tuple[str, str] | None = None
    for field in details:
        hint = _HINTS.get((exc.code, field))
        if hint is not None:
            break
    if hint is None:
        hint = _HINTS.get((exc.code, None))
    if hint is None:
        hint = (
            "EGG-API reported an error.",
            "See server logs for details, or contact your administrator.",
        )
    user_message, suggestion = hint
    return {
        "code": exc.code,
        "message": exc.message,
        "user_message": user_message,
        "suggestion": suggestion,
        "details": details,
    }


def translate_validation_error(exc: ValidationError) -> str:
    """Format a Pydantic ``ValidationError`` for a terminal reader.

    Collapses every sub-error into a single indented bullet list so
    operators can eyeball the field name and the reason without
    scrolling through a Python traceback.
    """
    lines = ["Configuration has the following problems:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "(root)"
        msg = str(err.get("msg", "invalid value"))
        lines.append(f"  - {loc}: {msg}")
    lines.append(
        "Fix these in config/egg.yaml (or re-run the setup wizard at "
        "/admin/ui/setup) and try again."
    )
    return "\n".join(lines)


def format_for_terminal(exc: Exception) -> str:
    """Best-effort rendering of *any* error for ``egg-api`` commands."""
    if isinstance(exc, AppError):
        payload = translate_app_error(exc)
        return (
            f"{payload['user_message']}\nHint: {payload['suggestion']}\n(code: {payload['code']})"
        )
    if isinstance(exc, ValidationError):
        return translate_validation_error(exc)
    # Fallback: keep the original representation but trim common noise.
    return f"Error: {exc}"
