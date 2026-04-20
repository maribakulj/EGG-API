from __future__ import annotations

from fastapi import Request

from app.dependencies import container
from app.errors import AppError

SESSION_COOKIE = "egg_admin_session"


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
