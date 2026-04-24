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
    create_ui_session_for_key_id,
    get_csrf_for_request,
    get_ui_key_id,
    verify_csrf,
)
from app.admin_ui.setup_service import (
    EXPOSURE_CATALOG,
    WIZARD_STEPS,
    SetupDraft,
    SetupDraftService,
    build_probe_adapter,
    discover_backend_candidates,
    draft_to_config,
    extract_index_choices,
    propose_mapping,
    run_probe_search,
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


def _require_login_key_id(request: Request) -> tuple[str, RedirectResponse | None]:
    """Return the signed-in ``key_id`` or a login redirect.

    Split from ``_require_login`` because the wizard needs the key_id
    itself (drafts are per-admin) whereas the rest of the UI only
    cares whether somebody is signed in.

    The first element is always a ``str`` (empty when the redirect is
    set) so call sites that gate on ``redirect is None`` don't need a
    secondary ``assert``-narrow for mypy.
    """
    key_id = get_ui_key_id(request)
    if key_id is None:
        return "", RedirectResponse("/admin/login", status_code=303)
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
    from app.i18n import resolve_lang, translator

    context.setdefault("current_key_id", get_ui_key_id(request))
    # Make the CSRF token available to every template so any form can include
    # it without each route explicitly passing it through the context.
    context.setdefault("csrf_token", get_csrf_for_request(request))
    # Sprint 30: plumb the locale + translator into every template so
    # nav labels, form headers and help text can flip between EN and FR.
    lang = resolve_lang(request)
    context.setdefault("lang", lang)
    context.setdefault("t", translator(lang))
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


@router.get("/setup-otp/{token}")
def setup_otp_exchange(token: str, request: Request):
    """Exchange a first-run OTP for an admin UI session.

    Minted by ``egg-api start``. The token is single-use, short-lived
    and hashed at rest; on success the caller is redirected to the
    setup wizard landing page with a session cookie set. On failure
    we render the login page so the operator can fall back to typing
    the bootstrap key.
    """
    key_id = container.store.consume_setup_otp(token)
    if key_id is None:
        return _render(
            "login.html",
            request,
            error=(
                "This one-time link has expired or was already used. "
                "Sign in with the bootstrap admin key printed by "
                "`egg-api start`, or mint a new link."
            ),
            status_code=401,
        )
    session = create_ui_session_for_key_id(key_id)
    response = RedirectResponse("/admin/ui/setup", status_code=303)
    _set_session_cookie(response, session)
    return response


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
        # Sprint 30: deployment-wide default language. Empty string clears
        # the preference (resolver falls back to env → English).
        default_language = (data.get("default_language") or "").strip().lower()
        if default_language in ("en", "fr"):
            cfg.default_language = default_language  # type: ignore[assignment]
        elif default_language == "":
            cfg.default_language = None

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
# Data imports (Sprint 22)
# ---------------------------------------------------------------------------


def _render_imports(
    request: Request,
    *,
    message: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    sources = container.store.list_import_sources()
    # Flatten the last 3 runs per source into one table so operators
    # can eyeball overall health without clicking into each source.
    recent_runs: list[dict[str, object]] = []
    label_by_id = {src.id: src.label for src in sources}
    for src in sources:
        for run in container.store.list_import_runs(src.id, limit=3):
            recent_runs.append(
                {
                    "source_label": label_by_id.get(run.source_id, f"#{run.source_id}"),
                    "started_at": run.started_at,
                    "ended_at": run.ended_at,
                    "status": run.status,
                    "records_ingested": run.records_ingested,
                    "records_failed": run.records_failed,
                    "error_message": run.error_message,
                }
            )
    recent_runs.sort(key=lambda r: str(r["started_at"]), reverse=True)
    return _render(
        "imports.html",
        request,
        status_code=status_code,
        sources=sources,
        recent_runs=recent_runs[:15],
        message=message,
        error=error,
    )


@router.get("/ui/imports", response_class=HTMLResponse)
def imports_page(request: Request):
    guard = _require_login(request)
    if guard is not None:
        return guard
    return _render_imports(request)


_ALLOWED_PROFILES = {"library", "museum", "archive", "custom"}
_ALLOWED_PREFIXES = {"oai_dc", "lido", "marcxml", "ead"}
# Sprint 24-26 extend the kind set: LIDO (OAI + flat), MARCXML,
# MARC (ISO 2709), CSV, then EAD (OAI + flat). The set is the
# single source of truth the template loops over.
_ALLOWED_KINDS = {
    "oaipmh",
    "oaipmh_lido",
    "oaipmh_marcxml",
    "oaipmh_ead",
    "lido_file",
    "marc_file",
    "marcxml_file",
    "csv_file",
    "ead_file",
}
_MARC_FLAVORS = {"marc21", "unimarc"}
_ALLOWED_SCHEDULES = {"hourly", "6h", "daily", "weekly"}


@router.post("/ui/imports/add", response_class=HTMLResponse)
async def imports_add(request: Request):
    guard = _require_login(request)
    if guard is not None:
        return guard
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    data = await _form(request)
    label = (data.get("label") or "").strip()
    kind = (data.get("kind") or "oaipmh").strip() or "oaipmh"
    url = (data.get("url") or "").strip()
    metadata_prefix = (data.get("metadata_prefix") or "oai_dc").strip() or "oai_dc"
    set_spec = (data.get("set_spec") or "").strip() or None
    schema_profile = (data.get("schema_profile") or "library").strip()

    if not label or not url:
        return _render_imports(
            request,
            error="Label and URL or file path are both required.",
            status_code=400,
        )
    if kind not in _ALLOWED_KINDS:
        return _render_imports(request, error="Unknown importer kind.", status_code=400)
    if schema_profile not in _ALLOWED_PROFILES:
        return _render_imports(request, error="Unknown schema profile.", status_code=400)
    # Per-kind handling of ``metadata_prefix``. For OAI-PMH kinds it
    # carries the OAI metadataPrefix; for MARC flat files it carries
    # the flavor (marc21 / unimarc); for LIDO / CSV flat files the
    # column is ignored by the dispatcher.
    flavor_hint = (data.get("marc_flavor") or "marc21").strip()
    if kind == "oaipmh_lido":
        metadata_prefix = "lido"
    elif kind == "oaipmh_marcxml":
        # Store the flavor on the row so the dispatcher knows how to
        # interpret the MARCXML tags; the OAI prefix is pinned to
        # "marcxml" by the dispatcher itself.
        if flavor_hint not in _MARC_FLAVORS:
            return _render_imports(request, error="Unknown MARC flavor.", status_code=400)
        metadata_prefix = flavor_hint
    elif kind in {"marc_file", "marcxml_file"}:
        if flavor_hint not in _MARC_FLAVORS:
            return _render_imports(request, error="Unknown MARC flavor.", status_code=400)
        metadata_prefix = flavor_hint
    elif kind == "oaipmh_ead":
        metadata_prefix = "ead"
    elif kind in {"lido_file", "csv_file", "ead_file"}:
        metadata_prefix = ""
    elif metadata_prefix not in _ALLOWED_PREFIXES:
        return _render_imports(request, error="Unsupported metadata prefix.", status_code=400)

    schedule = (data.get("schedule") or "").strip() or None
    if schedule is not None and schedule not in _ALLOWED_SCHEDULES:
        return _render_imports(request, error="Unknown schedule cadence.", status_code=400)
    next_run_at: str | None = None
    if schedule is not None:
        from app.scheduler import compute_next_run_at

        next_run_at = compute_next_run_at(schedule)

    container.store.add_import_source(
        label=label,
        kind=kind,
        url=url,
        metadata_prefix=metadata_prefix or None,
        set_spec=set_spec,
        schema_profile=schema_profile,
        schedule=schedule,
        next_run_at=next_run_at,
    )
    return _render_imports(request, message=f"Added source: {label}")


@router.post("/ui/imports/{source_id}/run", response_class=HTMLResponse)
async def imports_run(request: Request, source_id: int):
    from app.importers import run_import

    guard = _require_login(request)
    if guard is not None:
        return guard
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    src = container.store.get_import_source(source_id)
    if src is None or not src.url:
        return _render_imports(request, error="Unknown or unsupported source.", status_code=404)

    run_id = container.store.start_import_run(source_id)
    try:
        result = run_import(src, bulk_index=container.adapter.bulk_index)
    except ValueError as exc:
        container.store.finish_import_run(
            run_id,
            status="failed",
            records_ingested=0,
            records_failed=0,
            error_message=str(exc),
        )
        return _render_imports(request, error=f"Import failed: {exc}", status_code=400)
    except Exception as exc:
        container.store.finish_import_run(
            run_id,
            status="failed",
            records_ingested=0,
            records_failed=0,
            error_message=str(exc),
        )
        return _render_imports(request, error=f"Import failed: {exc}", status_code=500)

    status = "failed" if result.error else "succeeded"
    container.store.finish_import_run(
        run_id,
        status=status,
        records_ingested=result.ingested,
        records_failed=result.failed,
        error_message=result.error,
    )
    if result.error:
        return _render_imports(
            request,
            error=f"Import finished with errors: {result.error}",
            status_code=200,
        )
    return _render_imports(
        request,
        message=(
            f"Imported {result.ingested} record(s) from {src.label} ({result.failed} failure(s))."
        ),
    )


@router.post("/ui/imports/{source_id}/delete", response_class=HTMLResponse)
async def imports_delete(request: Request, source_id: int):
    guard = _require_login(request)
    if guard is not None:
        return guard
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error
    if not container.store.delete_import_source(source_id):
        return _render_imports(request, error="Unknown source.", status_code=404)
    return _render_imports(request, message="Source removed.")


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


@router.post("/ui/setup/language", response_class=HTMLResponse)
async def setup_language(request: Request):
    """Pick the deployment-wide UI language from the wizard landing screen.

    Sprint 30: sets ``AppConfig.default_language`` (every visitor
    without their own preference will see this language) and writes an
    ``egg_lang`` cookie so the operator who made the pick carries it
    through the rest of the wizard without re-selecting on every page.
    """

    guard = _require_login(request)
    if guard is not None:
        return guard
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    from app.i18n import LANG_COOKIE, SUPPORTED_LANGS

    data = await _form(request)
    lang = (data.get("lang") or "").strip().lower()
    if lang not in SUPPORTED_LANGS:
        cfg = container.config_manager.config
        row = container.store.load_setup_draft(get_ui_key_id(request) or "")
        return _render(
            "setup/landing.html",
            request,
            status_code=400,
            draft_exists=row is not None,
            current_step=None,
            has_active_config=bool(cfg.backend.url),
            cfg=cfg,
            error="Unsupported language selection.",
        )
    cfg = container.config_manager.config.model_copy(deep=True)
    cfg.default_language = lang  # type: ignore[assignment]
    container.reload(AppConfig.model_validate(cfg.model_dump(mode="python")))

    response = RedirectResponse("/admin/ui/setup", status_code=303)
    response.set_cookie(
        LANG_COOKIE,
        lang,
        max_age=365 * 24 * 3600,
        httponly=True,
        samesite="lax",
    )
    return response


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


@router.post("/ui/setup/discover", response_class=HTMLResponse)
async def setup_discover(request: Request):
    """Probe the well-known backend endpoints and re-render step 1.

    The operator does not have to fill anything in first; clicking the
    "Detect a backend" button triggers a parallel probe of localhost +
    the conventional docker-compose hostnames. Reachable candidates
    come back with an "Use this URL" button that pre-fills the form.
    """
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    draft, _ = _setup_service().load(key_id)
    candidates = discover_backend_candidates()
    ok_count = sum(1 for c in candidates if c.status == "ok")
    message = (
        f"Found {ok_count} reachable backend(s)."
        if ok_count
        else "No backend answered. Type the URL manually below."
    )
    return _render_wizard(
        request,
        "setup/backend.html",
        draft=draft,
        current_step="backend",
        message=message,
        discovery_candidates=candidates,
    )


@router.post("/ui/setup/discover/use", response_class=HTMLResponse)
async def setup_discover_use(request: Request):
    """Adopt one of the discovered URLs into the draft and save.

    The operator clicks "Use this URL" on a candidate; we patch only
    the backend type + URL into the draft (auth stays untouched) so
    they can jump straight to the "Test connection" / "Save & next"
    buttons with a sensible form pre-filled.
    """
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    data = await _form(request)
    chosen_url = (data.get("url") or "").strip()
    chosen_type = (data.get("backend_type") or "elasticsearch").strip() or "elasticsearch"
    if chosen_type not in {"elasticsearch", "opensearch"} or not chosen_url:
        return RedirectResponse("/admin/ui/setup/backend", status_code=303)

    svc = _setup_service()
    draft, _ = svc.load(key_id)
    draft.backend["type"] = chosen_type
    draft.backend["url"] = chosen_url
    svc.save(key_id, draft, "backend")
    return RedirectResponse("/admin/ui/setup/backend", status_code=303)


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
        draft.mapping = propose_mapping(
            draft.available_fields, profile=draft.schema_profile or "library"
        )
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
        draft.mapping = propose_mapping(
            draft.available_fields, profile=draft.schema_profile or "library"
        )
        _setup_service().save(key_id, draft, "mapping")
    return _render_wizard(request, "setup/mapping.html", draft=draft, current_step="mapping")


_PUBLIC_MAPPING_FIELDS: tuple[str, ...] = ("id", "type", "title", "description", "creators")
_MUSEUM_MAPPING_FIELDS: tuple[str, ...] = (
    "museum.inventory_number",
    "museum.artist",
    "museum.medium",
    "museum.dimensions",
    "museum.acquisition_date",
    "museum.current_location",
    "links.iiif_manifest",
    "links.thumbnail",
)
_ARCHIVE_MAPPING_FIELDS: tuple[str, ...] = (
    "archive.unit_id",
    "archive.unit_level",
    "archive.extent",
    "archive.repository",
    "archive.scope_content",
    "archive.access_conditions",
    "archive.parent_id",
)
_ALLOWED_SCHEMA_PROFILES: frozenset[str] = frozenset({"library", "museum", "archive", "custom"})


@router.post("/ui/setup/mapping/profile", response_class=HTMLResponse)
async def setup_mapping_profile(request: Request):
    """Swap the draft's schema_profile and rebuild the heuristic mapping.

    Sprint 23 lets the operator pick ``library`` / ``museum`` /
    ``archive`` / ``custom`` on the mapping screen. The switch
    wipes the current proposal, re-runs :func:`propose_mapping`
    against the backend's scanned fields and falls back to an
    empty mapping for ``custom`` (where the operator wants full
    manual control).
    """
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    data = await _form(request)
    profile = (data.get("schema_profile") or "library").strip()
    if profile not in _ALLOWED_SCHEMA_PROFILES:
        profile = "library"

    svc = _setup_service()
    draft, _ = svc.load(key_id)
    draft.schema_profile = profile
    if profile == "custom":
        draft.mapping = {}
    elif draft.available_fields:
        draft.mapping = propose_mapping(draft.available_fields, profile=profile)
    svc.save(key_id, draft, "mapping")
    return RedirectResponse("/admin/ui/setup/mapping", status_code=303)


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

    # Fields offered to the operator depend on the active profile.
    profile = draft.schema_profile or "library"
    public_fields: tuple[str, ...] = _PUBLIC_MAPPING_FIELDS
    if profile == "museum":
        public_fields = _PUBLIC_MAPPING_FIELDS + _MUSEUM_MAPPING_FIELDS
    elif profile == "archive":
        public_fields = _PUBLIC_MAPPING_FIELDS + _ARCHIVE_MAPPING_FIELDS
    new_mapping: dict[str, dict[str, object]] = {}
    for public in public_fields:
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
    svc.save(key_id, draft, "security")
    return RedirectResponse("/admin/ui/setup/security", status_code=303)


# -- Step 4: security ----------------------------------------------------


_ALLOWED_SECURITY_PROFILES: frozenset[str] = frozenset({"prudent", "standard"})
_ALLOWED_PUBLIC_MODES: frozenset[str] = frozenset(
    {"anonymous_allowed", "api_key_optional", "api_key_required"}
)


@router.get("/ui/setup/security", response_class=HTMLResponse)
def setup_security_page(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    draft, _ = _setup_service().load(key_id)
    if not draft.mapping:
        return RedirectResponse("/admin/ui/setup/mapping", status_code=303)
    return _render_wizard(request, "setup/security.html", draft=draft, current_step="security")


@router.post("/ui/setup/security", response_class=HTMLResponse)
async def setup_security_submit(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    data = await _form(request)
    svc = _setup_service()
    draft, _ = svc.load(key_id)
    profile = (data.get("security_profile") or "").strip()
    public_mode = (data.get("public_mode") or "").strip()
    if profile not in _ALLOWED_SECURITY_PROFILES or public_mode not in _ALLOWED_PUBLIC_MODES:
        return _render_wizard(
            request,
            "setup/security.html",
            draft=draft,
            current_step="security",
            error="Pick one security profile and one public-access mode before continuing.",
            status_code=400,
        )
    draft.security_profile = profile
    draft.public_mode = public_mode
    svc.save(key_id, draft, "exposure")
    return RedirectResponse("/admin/ui/setup/exposure", status_code=303)


# -- Step 5: exposure ----------------------------------------------------


@router.get("/ui/setup/exposure", response_class=HTMLResponse)
def setup_exposure_page(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    draft, _ = _setup_service().load(key_id)
    if not draft.security_profile:
        return RedirectResponse("/admin/ui/setup/security", status_code=303)
    return _render_wizard(
        request,
        "setup/exposure.html",
        draft=draft,
        current_step="exposure",
        catalog=EXPOSURE_CATALOG,
    )


@router.post("/ui/setup/exposure", response_class=HTMLResponse)
async def setup_exposure_submit(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    # The exposure form ships checkboxes, so ``_form`` (which collapses
    # to first-value) would drop repeat entries. Re-parse the body
    # keeping every value.
    raw = (await request.body()).decode("utf-8", errors="replace")
    parsed = parse_qs(raw, keep_blank_values=True)

    svc = _setup_service()
    draft, _ = svc.load(key_id)
    for field_name, allowed in EXPOSURE_CATALOG.items():
        submitted = parsed.get(field_name, [])
        # Constrain the operator's choice to the catalog we offered.
        draft.exposure[field_name] = [v for v in submitted if v in allowed]

    # At least ``id`` and ``type`` have to stay in allowed_include_fields,
    # otherwise the public API cannot return a usable record shape.
    include = draft.exposure.get("allowed_include_fields") or []
    for mandatory in ("id", "type"):
        if mandatory not in include:
            include = [*include, mandatory]
    draft.exposure["allowed_include_fields"] = include

    svc.save(key_id, draft, "keys")
    return RedirectResponse("/admin/ui/setup/keys", status_code=303)


# -- Step 6: first public key -------------------------------------------


@router.get("/ui/setup/keys", response_class=HTMLResponse)
def setup_keys_page(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    draft, _ = _setup_service().load(key_id)
    if not draft.security_profile:
        return RedirectResponse("/admin/ui/setup/security", status_code=303)
    return _render_wizard(request, "setup/keys.html", draft=draft, current_step="keys")


@router.post("/ui/setup/keys", response_class=HTMLResponse)
async def setup_keys_submit(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    data = await _form(request)
    svc = _setup_service()
    draft, _ = svc.load(key_id)
    action = data.get("action", "create")

    if action == "skip" or (action == "next" and draft.first_key is not None):
        svc.save(key_id, draft, "test")
        return RedirectResponse("/admin/ui/setup/test", status_code=303)

    if action == "create":
        label = (data.get("key_id") or "").strip()
        try:
            created = _key_service().create(label)
        except AppError as exc:
            return _render_wizard(
                request,
                "setup/keys.html",
                draft=draft,
                current_step="keys",
                error=exc.message,
                status_code=exc.status_code,
            )
        # Store the label + created_at + prefix so the published screen
        # can reference them; the raw secret is shown on this request
        # only, then wiped so the draft can never replay it.
        draft.first_key = {
            "key_id": created.key_id,
            "key": created.key,
            "created_at": created.created_at,
            "prefix": created.key[:8],
        }
        svc.save(key_id, draft, "keys")
        return _render_wizard(
            request,
            "setup/keys.html",
            draft=draft,
            current_step="keys",
            message="Key created. Copy the secret now — it will not be shown again.",
        )

    # Fallback: unknown action → just re-render the page.
    return _render_wizard(request, "setup/keys.html", draft=draft, current_step="keys")


# -- Step 7: live test ---------------------------------------------------


@router.get("/ui/setup/test", response_class=HTMLResponse)
def setup_test_page(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    draft, _ = _setup_service().load(key_id)
    if not draft.security_profile:
        return RedirectResponse("/admin/ui/setup/security", status_code=303)
    return _render_wizard(request, "setup/test.html", draft=draft, current_step="test")


@router.post("/ui/setup/test", response_class=HTMLResponse)
async def setup_test_submit(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    data = await _form(request)
    svc = _setup_service()
    draft, _ = svc.load(key_id)
    action = data.get("action", "next")

    if action == "run":
        query = (data.get("q") or "").strip()
        try:
            adapter = build_probe_adapter(draft)
            result = run_probe_search(adapter, query)
        except AppError as exc:
            return _render_wizard(
                request,
                "setup/test.html",
                draft=draft,
                current_step="test",
                error=f"Test failed: {exc.message}",
                status_code=exc.status_code,
            )
        except Exception:
            logger.exception("setup_test_failed", key_id=key_id)
            return _render_wizard(
                request,
                "setup/test.html",
                draft=draft,
                current_step="test",
                error=(
                    "Unexpected error while running the test. Re-check the "
                    "backend URL and credentials on step 1."
                ),
                status_code=400,
            )
        draft.test_result = result
        svc.save(key_id, draft, "test")
        return _render_wizard(
            request,
            "setup/test.html",
            draft=draft,
            current_step="test",
            message="Test completed.",
        )

    svc.save(key_id, draft, "done")
    return RedirectResponse("/admin/ui/setup/done", status_code=303)


# -- Step 8: review & publish -------------------------------------------


@router.get("/ui/setup/done", response_class=HTMLResponse)
def setup_done_page(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    draft, _ = _setup_service().load(key_id)
    if not draft.security_profile:
        return RedirectResponse("/admin/ui/setup/security", status_code=303)
    return _render_wizard(request, "setup/done.html", draft=draft, current_step="done")


@router.post("/ui/setup/publish", response_class=HTMLResponse)
async def setup_publish(request: Request):
    key_id, redirect = _require_login_key_id(request)
    if redirect is not None:
        return redirect
    csrf_error = await _enforce_csrf(request)
    if csrf_error is not None:
        return csrf_error

    svc = _setup_service()
    draft, _ = svc.load(key_id)
    try:
        new_config = draft_to_config(draft, preserve=container.config_manager.config)
    except Exception as exc:
        logger.exception("setup_publish_build_config_failed", key_id=key_id)
        return _render_wizard(
            request,
            "setup/done.html",
            draft=draft,
            current_step="done",
            error=f"Could not assemble a valid configuration: {exc}",
            status_code=400,
        )

    try:
        container.reload(new_config)
    except Exception as exc:
        logger.exception("setup_publish_reload_failed", key_id=key_id)
        return _render_wizard(
            request,
            "setup/done.html",
            draft=draft,
            current_step="done",
            error=f"Configuration saved but the service failed to swap to it: {exc}",
            status_code=500,
        )

    # Keep the key_id + prefix for the published page; the raw secret
    # was already shown on the keys step and is wiped here so the
    # draft never persists a replayable credential.
    shared_key = draft.first_key
    if draft.first_key and draft.first_key.get("key"):
        draft.first_key = {
            "key_id": draft.first_key["key_id"],
            "prefix": draft.first_key.get("prefix"),
            "created_at": draft.first_key.get("created_at"),
        }
    svc.reset(key_id)
    return _render(
        "setup/published.html",
        request,
        shared_key=shared_key,
    )


# -- Help / glossary ----------------------------------------------------


@router.get("/ui/help", response_class=HTMLResponse)
def help_glossary(request: Request):
    """Plain-language glossary for the terms the wizard uses."""
    guard = _require_login(request)
    if guard is not None:
        return guard
    return _render("help.html", request)
