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

from app.admin_ui.auth import (
    CSRF_FORM_FIELD,
    CSRF_HEADER,
    SESSION_COOKIE,
    clear_ui_session,
    create_ui_session_for_api_key,
    get_csrf_for_request,
    get_ui_key_id,
    verify_csrf,
)
from app.config.models import AppConfig
from app.dependencies import container
from app.errors import AppError
from app.logging import get_logger

logger = get_logger("egg.admin_ui")
router = APIRouter(prefix="/admin", tags=["admin-ui"])

_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
# Belt-and-suspenders: Jinja2Templates enables autoescape for .html/.htm by
# default, but we assert it explicitly so a future contributor cannot disable
# it accidentally.
templates.env.autoescape = True

_KEY_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


async def _form(request: Request) -> dict[str, str]:
    # _enforce_csrf stashes the parsed body when it runs; reuse it so the
    # downstream POST handler never tries to read the stream a second time.
    cached = getattr(request.state, "parsed_form", None)
    if cached is not None:
        return cached
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
    # Make the CSRF token available to every template so any form can include
    # it without each route explicitly passing it through the context.
    context.setdefault("csrf_token", get_csrf_for_request(request))
    return templates.TemplateResponse(request, template, context, status_code=status_code)


async def _enforce_csrf(request: Request) -> HTMLResponse | None:
    """Validate the CSRF token attached to a POST.

    Prefers the form field; falls back to an ``X-CSRF-Token`` header for
    fetch/XHR callers. Returns a 403 page on mismatch, ``None`` if the token
    is accepted.
    """
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)
    form_token = parsed.get(CSRF_FORM_FIELD, [""])[0]
    header_token = request.headers.get(CSRF_HEADER)
    submitted = form_token or header_token
    if not verify_csrf(request, submitted):
        return _render(
            "error.html",
            request,
            status_code=403,
            error=(
                "CSRF check failed. Reload the page and retry. If the problem "
                "persists, sign in again."
            ),
        )
    # Stash the already-parsed body so downstream handlers can reuse it
    # instead of consuming the request stream twice.
    request.state.parsed_form = {k: (v[0] if v else "") for k, v in parsed.items()}
    return None


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
async def logout(request: Request):
    """Invalidate the current session and redirect to the login page."""
    # CSRF-protected: a malicious cross-origin POST should not sign the user
    # out. Logged-out users hit the redirect anyway thanks to the fallback.
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error
    clear_ui_session(request)
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.post("/logout-everywhere")
async def logout_everywhere(request: Request):
    """Invalidate every active UI session for the currently signed-in key_id.

    Useful when the operator suspects a session leak on another device.
    """
    key_id = get_ui_key_id(request)
    if key_id is None:
        return RedirectResponse("/admin/login", status_code=303)
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error
    container.store.invalidate_sessions_for_key_id(key_id)
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
    except Exception:
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
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

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
        target_profile.allow_empty_query = data.get("allow_empty_query", "false").lower() == "true"
        target_profile.page_size_default = int(data.get("page_size_default", "20"))
        target_profile.page_size_max = int(data.get("page_size_max", "50"))
        target_profile.max_depth = int(data.get("max_depth", "2000"))

        valid, err = container.config_manager.validate_data(cfg.model_dump(mode="python"))
        if not valid:
            raise ValueError(err or "Invalid configuration")

        container.reload(AppConfig.model_validate(cfg.model_dump(mode="python")))
        return _render_config(request, message="Configuration saved successfully.")
    except Exception:
        # Detail goes to the structured log; the form stays generic so we
        # don't leak internal state (Pydantic traces, paths, DB errors) to
        # whatever browser happens to load the page.
        logger.exception("admin_config_update_failed")
        return _render_config(
            request,
            error="Unable to save configuration. Check the server logs for details.",
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
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

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
    except Exception:
        logger.exception("admin_create_key_failed", key_id=key_id_input)
        return _render_keys(
            request,
            error="Unable to create API key. Check the server logs for details.",
            status_code=400,
        )
    return _render_keys(
        request,
        message="API key created. Copy it now; it will not be shown again.",
        new_key=created,
    )


@router.post("/ui/keys/{key_id}/rotate", response_class=HTMLResponse)
async def rotate_key(request: Request, key_id: str):
    """Regenerate the raw secret behind ``key_id`` and invalidate its sessions.

    The new secret is rendered once in the flash panel; the operator must
    copy it or it is lost. All UI sessions tied to ``key_id`` are purged so
    the user is forced to sign in again with the new value.
    """
    guard = _require_login(request)
    if guard is not None:
        return guard
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error
    if not _KEY_ID_PATTERN.match(key_id):
        return _render_keys(request, error="Invalid key label.", status_code=400)

    new_secret = container.api_keys.rotate(key_id)
    if new_secret is None:
        return _render_keys(request, error=f"Unknown key label: {key_id}", status_code=404)
    container.store.invalidate_sessions_for_key_id(key_id)

    # Reuse the `new_key` slot to render the secret panel once.
    rotated = {"key_id": key_id, "key": new_secret}
    response = _render_keys(
        request,
        message=(
            f"Key '{key_id}' rotated. Copy the new secret now — it will not be "
            "shown again. Active sessions for this key have been revoked."
        ),
        new_key=rotated,
    )
    # When rotating the key we used to sign in, kick ourselves out too.
    if get_ui_key_id(request) == key_id:
        response.delete_cookie(SESSION_COOKIE)
    return response


@router.post("/ui/keys/{key_id}/status")
async def key_status_action(request: Request, key_id: str):
    """Activate, suspend, or revoke an existing API key."""
    guard = _require_login(request)
    if guard is not None:
        return guard
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error
    if not _KEY_ID_PATTERN.match(key_id):
        return RedirectResponse("/admin/ui/keys", status_code=303)
    data = await _form(request)
    action = data.get("action", "")
    if action == "revoke":
        container.api_keys.revoke_by_key_id(key_id)
    elif action == "suspend":
        container.api_keys.suspend_by_key_id(key_id)
    elif action == "activate":
        container.api_keys.activate_by_key_id(key_id)
    return RedirectResponse("/admin/ui/keys", status_code=303)


@router.get("/ui/usage", response_class=HTMLResponse)
def usage_page(request: Request):
    """Render the recent activity table (last 100 usage events)."""
    guard = _require_login(request)
    if guard is not None:
        return guard
    events = container.store.list_recent_usage_events(limit=100)
    return _render("usage.html", request, events=events)
