"""Admin web UI routes.

All HTML rendering goes through Jinja2 templates with autoescape enabled
(``select_autoescape(["html"])``). This prevents XSS injection when new
fields are added to the templates, because any untrusted value is escaped
by default. Do not build HTML via f-strings in this module.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.admin_ui.auth import SESSION_COOKIE, clear_ui_session, create_ui_session_for_api_key, get_ui_key_id
from app.config.models import AppConfig
from app.dependencies import container
from app.errors import AppError

router = APIRouter(prefix="/admin", tags=["admin-ui"])

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# Belt-and-suspenders: Jinja2Templates enables autoescape for .html/.htm by
# default, but we assert it explicitly so a future contributor cannot disable
# it accidentally.
templates.env.autoescape = True

_KEY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


async def _form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


def _render(
    template: str,
    request: Request,
    *,
    status_code: int = 200,
    **context,
) -> HTMLResponse:
    context.setdefault("current_key_id", get_ui_key_id(request))
    return templates.TemplateResponse(
        request, template, context, status_code=status_code
    )


def _set_session_cookie(response, token: str) -> None:
    auth_cfg = container.config_manager.config.auth
    samesite = (
        auth_cfg.admin_cookie_samesite
        if auth_cfg.admin_cookie_samesite in {"strict", "lax", "none"}
        else "strict"
    )
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        secure=auth_cfg.admin_cookie_secure,
        samesite=samesite,
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    """Render the admin sign-in form."""
    return _render("login.html", request)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    """Validate admin credentials and issue a session cookie.

    Rate-limited by client IP before credential verification to blunt
    credential-stuffing attacks.
    """
    client_ip = request.client.host if request.client else "anonymous"
    if not container.login_rate_limiter.allow(f"admin_login:{client_ip}"):
        return _render(
            "login.html",
            request,
            error="Too many attempts. Please try again later.",
            status_code=429,
        )

    data = await _form(request)
    try:
        token = create_ui_session_for_api_key(data.get("api_key", ""))
    except AppError:
        return _render(
            "login.html",
            request,
            error="Invalid admin API key.",
            status_code=401,
        )

    response = RedirectResponse("/admin/ui", status_code=303)
    _set_session_cookie(response, token)
    return response


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    """Invalidate the current session and redirect to the login page."""
    clear_ui_session(request)
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


def _require_login(request: Request) -> RedirectResponse | None:
    if not get_ui_key_id(request):
        return RedirectResponse("/admin/login", status_code=303)
    return None


@router.get("/ui", response_class=HTMLResponse)
def dashboard(request: Request):
    """Operator dashboard: backend + usage summary."""
    guard = _require_login(request)
    if guard is not None:
        return guard

    backend_status = "ok"
    try:
        container.adapter.health()
    except Exception:  # noqa: BLE001
        backend_status = "unavailable"

    usage = container.store.usage_summary()
    cfg = container.config_manager.config
    status = {
        "service": "running",
        "backend": backend_status,
        "usage": usage,
        "source": cfg.backend.index,
        "config_path": str(container.config_manager.path),
        "db_path": str(container.store.db_path),
    }
    return _render("dashboard.html", request, status=status)


@router.get("/ui/config", response_class=HTMLResponse)
def config_page(request: Request):
    """Render the configuration editor."""
    guard = _require_login(request)
    if guard is not None:
        return guard
    return _render_config(request)


def _render_config(
    request: Request,
    *,
    message: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    cfg = container.config_manager.config
    profile = cfg.profiles[cfg.security_profile]
    return _render(
        "config.html",
        request,
        status_code=status_code,
        cfg=cfg,
        profile=profile,
        message=message,
        error=error,
    )


@router.post("/ui/config", response_class=HTMLResponse)
async def config_update(request: Request):
    """Persist a configuration change submitted from the UI form."""
    guard = _require_login(request)
    if guard is not None:
        return guard

    data = await _form(request)
    try:
        cfg = container.config_manager.config.model_copy(deep=True)
        cfg.backend.url = data.get("backend_url", "").strip()
        cfg.backend.index = data.get("backend_index", "").strip()
        cfg.security_profile = data.get("security_profile", "")
        cfg.auth.public_mode = data.get("public_mode", "")
        cfg.storage.sqlite_path = data.get("sqlite_path", "").strip()

        if cfg.security_profile not in cfg.profiles:
            raise ValueError("Unknown security profile")

        target_profile = cfg.profiles[cfg.security_profile]
        target_profile.allow_empty_query = (
            data.get("allow_empty_query", "false").lower() == "true"
        )
        target_profile.page_size_default = int(data.get("page_size_default", "20"))
        target_profile.page_size_max = int(data.get("page_size_max", "50"))
        target_profile.max_depth = int(data.get("max_depth", "2000"))

        valid, err = container.config_manager.validate_data(cfg.model_dump(mode="python"))
        if not valid:
            raise ValueError(err or "Invalid configuration")

        container.reload(AppConfig.model_validate(cfg.model_dump(mode="python")))
        return _render_config(request, message="Configuration saved successfully.")
    except Exception as exc:  # noqa: BLE001
        return _render_config(
            request,
            error=f"Unable to save configuration: {exc}",
            status_code=400,
        )


@router.get("/ui/mapping", response_class=HTMLResponse)
def mapping_page(request: Request):
    """Render the field-mapping overview."""
    guard = _require_login(request)
    if guard is not None:
        return guard

    cfg = container.config_manager.config
    return _render(
        "mapping.html",
        request,
        mapping=cfg.mapping,
        allowed_filters=sorted(container.policy.filter_params),
        allowed_facets=cfg.allowed_facets,
        allowed_sorts=cfg.allowed_sorts,
        allowed_include_fields=cfg.allowed_include_fields,
    )


@router.get("/ui/keys", response_class=HTMLResponse)
def keys_page(request: Request):
    """Render the API key management page."""
    guard = _require_login(request)
    if guard is not None:
        return guard
    return _render_keys(request)


def _render_keys(
    request: Request,
    *,
    message: str | None = None,
    error: str | None = None,
    new_key: object | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _render(
        "keys.html",
        request,
        status_code=status_code,
        keys=container.api_keys.list_keys(),
        message=message,
        error=error,
        new_key=new_key,
    )


@router.post("/ui/keys/create", response_class=HTMLResponse)
async def create_key(request: Request):
    """Create a new API key. The raw secret is rendered once and never stored."""
    guard = _require_login(request)
    if guard is not None:
        return guard

    data = await _form(request)
    key_id_input = data.get("key_id", "").strip()
    if not _KEY_ID_PATTERN.match(key_id_input):
        return _render_keys(
            request,
            error=(
                "Key label must be 1-64 characters and contain only letters, "
                "digits, '.', '_' or '-'."
            ),
            status_code=400,
        )

    try:
        created = container.api_keys.create(key_id_input)
    except Exception as exc:  # noqa: BLE001
        return _render_keys(
            request,
            error=f"Unable to create API key: {exc}",
            status_code=400,
        )
    return _render_keys(
        request,
        message="API key created. Copy it now; it will not be shown again.",
        new_key=created,
    )


@router.post("/ui/keys/{key_id}/status")
async def key_status_action(request: Request, key_id: str) -> RedirectResponse:
    """Activate, suspend, or revoke an existing API key."""
    guard = _require_login(request)
    if guard is not None:
        return guard
    if not _KEY_ID_PATTERN.match(key_id):
        return RedirectResponse("/admin/ui/keys", status_code=303)
    data = await _form(request)
    action = data.get("action", "")
    if action == "revoke":
        container.api_keys.revoke(key_id)
    elif action == "suspend":
        container.api_keys.suspend(key_id)
    elif action == "activate":
        container.api_keys.activate(key_id)
    return RedirectResponse("/admin/ui/keys", status_code=303)


@router.get("/ui/usage", response_class=HTMLResponse)
def usage_page(request: Request):
    """Render the recent activity table (last 100 usage events)."""
    guard = _require_login(request)
    if guard is not None:
        return guard
    events = container.store.list_recent_usage_events(limit=100)
    return _render("usage.html", request, events=events)
