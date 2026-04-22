"""Admin web UI routes.

All HTML rendering goes through Jinja2 templates with autoescape enabled
(``select_autoescape(["html"])``). This prevents XSS injection when new
fields are added to the templates, because any untrusted value is escaped
by default. Do not build HTML via f-strings in this module.
"""

from __future__ import annotations

import contextlib
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
from app.admin_ui.setup_service import (
    WIZARD_STEPS,
    SetupDraft,
    SetupDraftService,
    build_probe_adapter,
    extract_index_choices,
    propose_mapping,
)
from app.auth.key_service import ApiKeyService
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


def _key_service() -> ApiKeyService:
    # Resolve per request so Container.reload() hot-swaps take effect.
    return ApiKeyService(container.api_keys, container.store)


def _setup_service() -> SetupDraftService:
    return SetupDraftService(container.store)


def _require_login_key_id(request: Request) -> tuple[str, None] | tuple[None, RedirectResponse]:
    """Return the signed-in ``key_id`` or a login redirect.

    Split from ``_require_login`` because the wizard needs the key_id
    itself (drafts are per-admin) whereas the rest of the UI only
    cares whether somebody is signed in.
    """
    key_id = get_ui_key_id(request)
    if key_id is None:
        return None, RedirectResponse("/admin/login", status_code=303)
    return key_id, None


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
        # `public_mode` is a Literal on the model; the actual value is
        # validated below via model_validate, so the mypy-visible assignment
        # is intentionally broadened — invalid strings still surface as a
        # user-visible error.
        cfg.auth.public_mode = data.get("public_mode", "")  # type: ignore[assignment]
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
        keys=_key_service().list_keys(),
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
    try:
        created = _key_service().create(key_id_input)
    except AppError as exc:
        return _render_keys(request, error=exc.message, status_code=exc.status_code)
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

    try:
        rotated = _key_service().rotate(key_id)
    except AppError as exc:
        return _render_keys(request, error=exc.message, status_code=exc.status_code)

    response = _render_keys(
        request,
        message=(
            f"Key '{key_id}' rotated. Copy the new secret now — it will not be "
            "shown again. Active sessions for this key have been revoked."
        ),
        # Template reads .key_id / .key; the dataclass exposes both.
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
    data = await _form(request)
    action = data.get("action", "")
    # Legacy UI behaviour: ignore malformed/unknown inputs and bounce
    # back to the list page. The REST API surfaces a typed error.
    with contextlib.suppress(AppError):
        _key_service().set_status(key_id, action)
    return RedirectResponse("/admin/ui/keys", status_code=303)


@router.get("/ui/usage", response_class=HTMLResponse)
def usage_page(request: Request):
    """Render the recent activity table (last 100 usage events)."""
    guard = _require_login(request)
    if guard is not None:
        return guard
    events = container.store.list_recent_usage_events(limit=100)
    return _render("usage.html", request, events=events)


# ---------------------------------------------------------------------------
# Setup wizard (Sprint 14 — screens 1-4)
#
# Per SPECS §26 the wizard is the non-technical operator's first contact
# with the product. Every screen is a server-rendered form with one
# clear action; progress is persisted in ``setup_drafts`` scoped by the
# signed-in admin ``key_id`` so a refresh or tab close never loses work.
# Nothing written here reaches ``egg.yaml`` — Sprint 15 adds the final
# "Publish" step.
# ---------------------------------------------------------------------------


def _render_wizard(
    request: Request,
    template: str,
    *,
    draft: SetupDraft,
    current_step: str,
    message: str | None = None,
    error: str | None = None,
    status_code: int = 200,
    **extra: object,
) -> HTMLResponse:
    return _render(
        template,
        request,
        status_code=status_code,
        draft=draft,
        current_step=current_step,
        message=message,
        error=error,
        **extra,
    )


@router.get("/ui/setup", response_class=HTMLResponse)
def setup_landing(request: Request):
    """Wizard landing page: propose to start or resume a draft."""
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    _, step = _setup_service().load(key_id)
    row = container.store.load_setup_draft(key_id)
    cfg = container.config_manager.config
    return _render(
        "setup/landing.html",
        request,
        draft_exists=row is not None,
        current_step=step,
        has_active_config=bool(cfg.backend.url),
        cfg=cfg,
    )


@router.post("/ui/setup/start", response_class=HTMLResponse)
async def setup_start(request: Request):
    """Create an empty draft and jump to the first step."""
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error
    _setup_service().save(key_id, SetupDraft(), WIZARD_STEPS[0])
    return RedirectResponse("/admin/ui/setup/backend", status_code=303)


@router.post("/ui/setup/reset", response_class=HTMLResponse)
async def setup_reset(request: Request):
    """Discard the current draft and bounce back to the landing screen."""
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error
    _setup_service().reset(key_id)
    return RedirectResponse("/admin/ui/setup", status_code=303)


# -- Step 1: backend ------------------------------------------------------


def _draft_from_backend_form(data: dict[str, str], previous: SetupDraft) -> SetupDraft:
    """Apply step-1 form fields to the draft, preserving stashed secrets.

    Inline secrets (``auth.password`` / ``auth.token``) are kept when
    the operator submits the form with the field blank — otherwise
    navigating to the next step would wipe a valid inline credential.
    """
    previous_auth = previous.backend.get("auth") or {}
    mode = (data.get("auth_mode") or "none").strip() or "none"
    submitted_password = data.get("auth_password") or ""
    submitted_token = data.get("auth_token") or ""
    auth: dict[str, object] = {"mode": mode}
    if mode == "basic":
        auth["username"] = (data.get("auth_username") or "").strip() or None
        auth["password_env"] = (data.get("auth_password_env") or "").strip() or None
        auth["password"] = submitted_password or previous_auth.get("password") or None
    if mode in {"bearer", "api_key"}:
        auth["token_env"] = (data.get("auth_token_env") or "").strip() or None
        auth["token"] = submitted_token or previous_auth.get("token") or None
    draft = SetupDraft(
        backend={
            "type": (data.get("backend_type") or "elasticsearch").strip(),
            "url": (data.get("backend_url") or "").strip(),
            "auth": auth,
        },
        source=dict(previous.source),
        detected_version=previous.detected_version,
        available_indices=list(previous.available_indices),
        available_fields=dict(previous.available_fields),
        mapping={k: dict(v) for k, v in previous.mapping.items()},
    )
    return draft


@router.get("/ui/setup/backend", response_class=HTMLResponse)
def setup_backend_page(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    draft, _ = _setup_service().load(key_id)
    return _render_wizard(request, "setup/backend.html", draft=draft, current_step="backend")


@router.post("/ui/setup/backend", response_class=HTMLResponse)
async def setup_backend_submit(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    data = await _form(request)
    svc = _setup_service()
    previous, _ = svc.load(key_id)
    try:
        draft = _draft_from_backend_form(data, previous)
    except AppError as exc:
        return _render_wizard(
            request,
            "setup/backend.html",
            draft=previous,
            current_step="backend",
            error=exc.message,
            status_code=exc.status_code,
        )

    action = data.get("action", "next")
    if action == "test":
        # Probe the candidate backend without committing the step.  On
        # success we record the version so the operator has feedback;
        # on failure we show the error but keep the form values.
        try:
            adapter = build_probe_adapter(draft)
            probe = adapter.detect()
        except AppError as exc:
            svc.save(key_id, draft, "backend")
            return _render_wizard(
                request,
                "setup/backend.html",
                draft=draft,
                current_step="backend",
                error=f"Could not reach the backend: {exc.message}",
                status_code=400,
            )
        except Exception:
            svc.save(key_id, draft, "backend")
            logger.exception("setup_backend_probe_failed", key_id=key_id)
            return _render_wizard(
                request,
                "setup/backend.html",
                draft=draft,
                current_step="backend",
                error=(
                    "Unexpected error while contacting the backend. "
                    "Check the URL, credentials, and server logs."
                ),
                status_code=400,
            )
        version_info = probe.get("version", {}) if isinstance(probe, dict) else {}
        draft.detected_version = str(version_info.get("number") or "") or None
        svc.save(key_id, draft, "backend")
        return _render_wizard(
            request,
            "setup/backend.html",
            draft=draft,
            current_step="backend",
            message="Connection successful.",
            probe_result={"version": draft.detected_version or "(unknown)"},
        )

    # action == "next"
    if not draft.backend.get("url"):
        return _render_wizard(
            request,
            "setup/backend.html",
            draft=draft,
            current_step="backend",
            error="Backend URL is required.",
            status_code=400,
        )
    svc.save(key_id, draft, "source")
    return RedirectResponse("/admin/ui/setup/source", status_code=303)


# -- Step 2: source -------------------------------------------------------


@router.get("/ui/setup/source", response_class=HTMLResponse)
def setup_source_page(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    draft, _ = _setup_service().load(key_id)
    if not draft.backend.get("url"):
        return RedirectResponse("/admin/ui/setup/backend", status_code=303)
    return _render_wizard(request, "setup/source.html", draft=draft, current_step="source")


@router.post("/ui/setup/source", response_class=HTMLResponse)
async def setup_source_submit(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    data = await _form(request)
    svc = _setup_service()
    draft, _ = svc.load(key_id)
    if not draft.backend.get("url"):
        return RedirectResponse("/admin/ui/setup/backend", status_code=303)

    draft.source["index"] = (data.get("index") or "").strip()
    action = data.get("action", "next")

    if action == "scan":
        try:
            adapter = build_probe_adapter(draft)
            payload = adapter.scan_fields()
        except AppError as exc:
            svc.save(key_id, draft, "source")
            return _render_wizard(
                request,
                "setup/source.html",
                draft=draft,
                current_step="source",
                error=f"Could not scan the backend: {exc.message}",
                status_code=exc.status_code,
            )
        except Exception:
            svc.save(key_id, draft, "source")
            logger.exception("setup_scan_failed", key_id=key_id)
            return _render_wizard(
                request,
                "setup/source.html",
                draft=draft,
                current_step="source",
                error="Unexpected error while scanning the backend.",
                status_code=400,
            )
        indices, fields = extract_index_choices(payload if isinstance(payload, dict) else {})
        draft.available_indices = indices
        draft.available_fields = fields
        if not draft.source["index"] and len(indices) == 1:
            draft.source["index"] = indices[0]
        svc.save(key_id, draft, "source")
        return _render_wizard(
            request,
            "setup/source.html",
            draft=draft,
            current_step="source",
            message=f"Found {len(indices)} index(es) and {len(fields)} field(s).",
        )

    # action == "next"
    if not draft.source.get("index"):
        return _render_wizard(
            request,
            "setup/source.html",
            draft=draft,
            current_step="source",
            error="Please enter or pick an index before continuing.",
            status_code=400,
        )
    # Pre-fill a mapping proposal once we land on step 3 — only when the
    # operator hasn't started editing one yet.
    if not draft.mapping and draft.available_fields:
        draft.mapping = propose_mapping(draft.available_fields)
    svc.save(key_id, draft, "mapping")
    return RedirectResponse("/admin/ui/setup/mapping", status_code=303)


# -- Step 3: mapping ------------------------------------------------------


@router.get("/ui/setup/mapping", response_class=HTMLResponse)
def setup_mapping_page(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    draft, _ = _setup_service().load(key_id)
    if not draft.source.get("index"):
        return RedirectResponse("/admin/ui/setup/source", status_code=303)
    if not draft.mapping and draft.available_fields:
        draft.mapping = propose_mapping(draft.available_fields)
        _setup_service().save(key_id, draft, "mapping")
    return _render_wizard(request, "setup/mapping.html", draft=draft, current_step="mapping")


_PUBLIC_MAPPING_FIELDS: tuple[str, ...] = ("id", "type", "title", "description", "creators")


@router.post("/ui/setup/mapping", response_class=HTMLResponse)
async def setup_mapping_submit(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    data = await _form(request)
    svc = _setup_service()
    draft, _ = svc.load(key_id)
    if not draft.source.get("index"):
        return RedirectResponse("/admin/ui/setup/source", status_code=303)

    new_mapping: dict[str, dict[str, object]] = {}
    for public in _PUBLIC_MAPPING_FIELDS:
        source = (data.get(f"source__{public}") or "").strip()
        mode = (data.get(f"mode__{public}") or "direct").strip() or "direct"
        if not source:
            continue
        rule: dict[str, object] = {
            "source": source,
            "mode": mode,
            "criticality": "required" if public in {"id", "type"} else "optional",
        }
        if mode == "split_list":
            rule["separator"] = ";"
        new_mapping[public] = rule

    if not new_mapping.get("id") or not new_mapping.get("type"):
        draft.mapping = new_mapping
        svc.save(key_id, draft, "mapping")
        return _render_wizard(
            request,
            "setup/mapping.html",
            draft=draft,
            current_step="mapping",
            error="The 'id' and 'type' fields are required — pick a backend field for each.",
            status_code=400,
        )

    draft.mapping = new_mapping
    svc.save(key_id, draft, "mapping")
    return _render_wizard(
        request,
        "setup/mapping.html",
        draft=draft,
        current_step="mapping",
        message=(
            "Mapping saved. Screens 4-7 (security profile, exposure, keys, "
            "test) arrive in Sprint 15."
        ),
    )
