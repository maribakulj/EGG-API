from __future__ import annotations

import hmac
from hashlib import sha256

from fastapi import Request

from app.dependencies import container
from app.errors import AppError
from app.runtime_paths import resolve_csrf_signing_key

SESSION_COOKIE = "egg_admin_session"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"

# Persistent signing key loaded from the EGG_HOME sidecar (or the
# ``EGG_CSRF_SIGNING_KEY`` env var). Pre-Sprint-10 we regenerated this at
# every import, which meant a restart silently invalidated every open
# admin tab and made multi-node deploys impossible without extra config.
# The sidecar is 0600 under ``EGG_HOME/data/csrf_signing.key``; operators
# wanting to share it across nodes should set the env var.
_CSRF_SIGNING_KEY = resolve_csrf_signing_key()


def _csrf_for_session(session_token: str) -> str:
    """Derive a stable CSRF token from the session cookie value."""
    mac = hmac.new(_CSRF_SIGNING_KEY, session_token.encode(), sha256)
    return mac.hexdigest()


def create_ui_session_for_api_key(api_key: str) -> str:
    identity = container.api_keys.get_identity(api_key)
    if not identity:
        raise AppError("invalid_api_key", "Invalid admin API key", status_code=401)
    ttl_hours = container.config_manager.config.auth.admin_session_ttl_hours
    return container.store.create_ui_session(identity.key_id, ttl_hours=ttl_hours)


def get_ui_key_id(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    return container.store.get_ui_session_key_id(token)


def require_ui_session(request: Request) -> str:
    key_id = get_ui_key_id(request)
    if not key_id:
        raise AppError("forbidden", "Admin login required", status_code=403)
    return key_id


def clear_ui_session(request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    container.store.delete_ui_session(token)


def get_csrf_for_request(request: Request) -> str | None:
    """Return the expected CSRF token for the current session, or None."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return _csrf_for_session(token)


def verify_csrf(request: Request, submitted_token: str | None) -> bool:
    expected = get_csrf_for_request(request)
    if expected is None or not submitted_token:
        return False
    return hmac.compare_digest(expected, submitted_token)
