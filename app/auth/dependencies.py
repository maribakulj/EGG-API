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
    identity = container.api_keys.get_identity(x_api_key) if x_api_key else None

    if mode == "api_key_required" and identity is None:
        raise AppError("invalid_api_key", "Public API key required", status_code=401)
    if mode == "api_key_optional" and x_api_key and identity is None:
        raise AppError("invalid_api_key", "Invalid public API key", status_code=401)

    # Never use the raw API key as the rate-limit bucket subject: it would
    # persist the secret in the limiter's in-memory structures and let an
    # attacker cycle keys to dodge the quota. Prefer the resolved key_id,
    # fall back to the client IP, and only use "anonymous" when we have
    # neither.
    if identity is not None:
        subject: str = f"key:{identity.key_id}"
    elif request.client is not None:
        subject = f"ip:{request.client.host}"
    else:
        subject = "anonymous"

    if not container.rate_limiter.allow(subject):
        raise AppError("quota_exceeded", "Rate limit exceeded", status_code=429)
