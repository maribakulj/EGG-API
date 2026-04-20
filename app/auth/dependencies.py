from __future__ import annotations

from fastapi import Header, Request

from app.dependencies import container
from app.errors import AppError


def require_admin_key(x_api_key: str | None = Header(default=None)) -> str:
    if not container.api_keys.validate(x_api_key):
        raise AppError("invalid_api_key", "Invalid admin API key", status_code=401)
    return x_api_key or ""


def enforce_public_auth(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    mode = container.config_manager.config.auth.public_mode
    if mode == "api_key_required" and not container.api_keys.validate(x_api_key):
        raise AppError("invalid_api_key", "Public API key required", status_code=401)
    if mode == "api_key_optional" and x_api_key and not container.api_keys.validate(x_api_key):
        raise AppError("invalid_api_key", "Invalid public API key", status_code=401)

    subject = x_api_key or request.client.host if request.client else "anonymous"
    if not container.rate_limiter.allow(str(subject)):
        raise AppError("quota_exceeded", "Rate limit exceeded", status_code=429)
