from __future__ import annotations

from html import escape
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.admin_ui.auth import SESSION_COOKIE, clear_ui_session, create_ui_session_for_api_key, get_ui_key_id
from app.config.models import AppConfig
from app.dependencies import container
from app.errors import AppError

router = APIRouter(prefix="/admin", tags=["admin-ui"])


def _layout(title: str, body: str, current_key_id: str | None) -> str:
    nav = ""
    if current_key_id:
        nav = (
            '<nav><a href="/admin/ui">Dashboard</a>'
            '<a href="/admin/ui/config">Configuration</a>'
            '<a href="/admin/ui/mapping">Mapping</a>'
            '<a href="/admin/ui/keys">API keys</a>'
            '<a href="/admin/ui/usage">Recent activity</a>'
            '<form action="/admin/logout" method="post" class="inline"><button type="submit">Sign out</button></form></nav>'
        )
    return f"""
<!doctype html>
<html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>{escape(title)}</title>
<link rel='stylesheet' href='/admin-static/admin.css'>
</head><body>
<header><h1>PISCO-API Admin</h1>{'<p>Signed in as <strong>'+escape(current_key_id)+'</strong></p>' if current_key_id else ''}</header>
{nav}
<main>{body}</main>
</body></html>
"""


def _page(request: Request, title: str, body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(_layout(title, body, get_ui_key_id(request)), status_code=status_code)


async def _form(request: Request) -> dict[str, str]:
    raw = (await request.body()).decode()
    parsed = parse_qs(raw, keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in parsed.items()}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    body = """
<h2>Admin sign in</h2>
<p>Use your admin API key to access the web console.</p>
<form method='post' action='/admin/login'>
<label>Admin API key <input name='api_key' type='password' required></label>
<button type='submit'>Sign in</button>
</form>
"""
    return _page(request, "Admin sign in", body)


@router.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request) -> HTMLResponse:
    data = await _form(request)
    try:
        token = create_ui_session_for_api_key(data.get("api_key", ""))
    except AppError:
        body = """
<h2>Admin sign in</h2>
<p class='error'>Invalid admin API key.</p>
<form method='post' action='/admin/login'>
<label>Admin API key <input name='api_key' type='password' required></label>
<button type='submit'>Sign in</button>
</form>
"""
        return _page(request, "Admin sign in", body, status_code=401)

    response = RedirectResponse("/admin/ui", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=False, samesite="lax")
    return response


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    clear_ui_session(request)
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response


@router.get("/ui", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    key_id = get_ui_key_id(request)
    if not key_id:
        return RedirectResponse("/admin/login", status_code=303)

    backend_status = "ok"
    try:
        container.adapter.health()
    except Exception:  # noqa: BLE001
        backend_status = "unavailable"

    usage = container.store.usage_summary()
    body = f"""
<h2>Dashboard</h2>
<div class='grid'>
<div class='card'><h3>Service</h3><p>running</p></div>
<div class='card'><h3>Backend connection</h3><p>{escape(backend_status)}</p></div>
<div class='card'><h3>Active API keys</h3><p>{usage['active_keys']}</p></div>
<div class='card'><h3>Recent requests</h3><p>{usage['events']}</p></div>
<div class='card'><h3>Recent errors</h3><p>{usage['errors']}</p></div>
</div>
<ul>
<li><strong>Config file:</strong> {escape(str(container.config_manager.path))}</li>
<li><strong>State database:</strong> {escape(str(container.store.db_path))}</li>
<li><strong>Source index:</strong> {escape(container.config_manager.config.backend.index)}</li>
</ul>
"""
    return _page(request, "Dashboard", body)


@router.get("/ui/config", response_class=HTMLResponse)
def config_page(request: Request) -> HTMLResponse:
    if not get_ui_key_id(request):
        return RedirectResponse("/admin/login", status_code=303)
    return _config_page(request)


def _config_page(request: Request, message: str | None = None, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    cfg = container.config_manager.config
    profile = cfg.profiles[cfg.security_profile]
    msg = f"<p class='success'>{escape(message)}</p>" if message else ""
    err = f"<p class='error'>{escape(error)}</p>" if error else ""
    profile_options = "".join(
        f"<option value='{escape(name)}' {'selected' if name == cfg.security_profile else ''}>{escape(name)}</option>"
        for name in cfg.profiles
    )
    body = f"""
<h2>Configuration</h2>
<p>Edit core settings and save safely.</p>
{msg}{err}
<form method='post' action='/admin/ui/config' class='stack'>
<label>Backend URL <input name='backend_url' value='{escape(cfg.backend.url)}' required></label>
<label>Source index <input name='backend_index' value='{escape(cfg.backend.index)}' required></label>
<label>Security profile <select name='security_profile'>{profile_options}</select></label>
<label>Public access
<select name='public_mode'>
<option value='anonymous_allowed' {'selected' if cfg.auth.public_mode == 'anonymous_allowed' else ''}>Anonymous allowed</option>
<option value='api_key_optional' {'selected' if cfg.auth.public_mode == 'api_key_optional' else ''}>API key optional</option>
<option value='api_key_required' {'selected' if cfg.auth.public_mode == 'api_key_required' else ''}>API key required</option>
</select></label>
<label>State DB path <input name='sqlite_path' value='{escape(cfg.storage.sqlite_path)}' required></label>
<label>Allow empty query
<select name='allow_empty_query'>
<option value='false' {'selected' if not profile.allow_empty_query else ''}>No</option>
<option value='true' {'selected' if profile.allow_empty_query else ''}>Yes</option>
</select></label>
<label>Page size default <input name='page_size_default' type='number' min='1' value='{profile.page_size_default}' required></label>
<label>Page size max <input name='page_size_max' type='number' min='1' value='{profile.page_size_max}' required></label>
<label>Max depth <input name='max_depth' type='number' min='1' value='{profile.max_depth}' required></label>
<button type='submit'>Save configuration</button>
</form>
"""
    return _page(request, "Configuration", body, status_code=status_code)


@router.post("/ui/config", response_class=HTMLResponse)
async def config_update(request: Request) -> HTMLResponse:
    if not get_ui_key_id(request):
        return RedirectResponse("/admin/login", status_code=303)

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
        return _config_page(request, message="Configuration saved successfully.")
    except Exception as exc:  # noqa: BLE001
        return _config_page(request, error=f"Unable to save configuration: {exc}", status_code=400)


@router.get("/ui/mapping", response_class=HTMLResponse)
def mapping_page(request: Request) -> HTMLResponse:
    if not get_ui_key_id(request):
        return RedirectResponse("/admin/login", status_code=303)

    cfg = container.config_manager.config
    rows = "".join(
        f"<tr><td>{escape(field)}</td><td>{escape(rule.mode)}</td><td>{escape(rule.source or '-')}</td><td>{escape(rule.criticality)}</td></tr>"
        for field, rule in cfg.mapping.items()
    )
    body = f"""
<h2>Mapping and exposure</h2>
<h3>Mapped public fields</h3>
<table><thead><tr><th>Public field</th><th>Mode</th><th>Source</th><th>Criticality</th></tr></thead><tbody>{rows}</tbody></table>
<h3>Allowed filters</h3><p>{escape(', '.join(sorted(container.policy.filter_params)))}</p>
<h3>Allowed facets</h3><p>{escape(', '.join(cfg.allowed_facets))}</p>
<h3>Allowed sorts</h3><p>{escape(', '.join(cfg.allowed_sorts))}</p>
<h3>Allowed include fields</h3><p>{escape(', '.join(cfg.allowed_include_fields))}</p>
"""
    return _page(request, "Mapping", body)


@router.get("/ui/keys", response_class=HTMLResponse)
def keys_page(request: Request) -> HTMLResponse:
    if not get_ui_key_id(request):
        return RedirectResponse("/admin/login", status_code=303)
    return _keys_page(request)


def _keys_page(
    request: Request,
    *,
    message: str | None = None,
    error: str | None = None,
    new_key_label: str | None = None,
    new_key_secret: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    msg = f"<p class='success'>{escape(message)}</p>" if message else ""
    err = f"<p class='error'>{escape(error)}</p>" if error else ""
    secret_box = ""
    if new_key_secret and new_key_label:
        secret_box = (
            "<div class='secret-box'><p><strong>New key created:</strong> "
            + escape(new_key_label)
            + "</p><p><strong>Secret (copy now):</strong> <code>"
            + escape(new_key_secret)
            + "</code></p></div>"
        )

    key_rows = "".join(
        (
            f"<tr><td>{escape(k.key_id)}</td><td>{escape(k.status)}</td><td>{escape(k.created_at)}</td>"
            f"<td>{escape(k.last_used_at or '-')}</td><td>"
            f"<form class='inline' method='post' action='/admin/ui/keys/{escape(k.key_id)}/status'>"
            "<button name='action' value='activate' type='submit'>Activate</button>"
            "<button name='action' value='suspend' type='submit'>Suspend</button>"
            "<button name='action' value='revoke' type='submit'>Revoke</button>"
            "</form></td></tr>"
        )
        for k in container.api_keys.list_keys()
    )

    body = f"""
<h2>API keys</h2>
{msg}{err}
<form method='post' action='/admin/ui/keys/create' class='inline'>
<label>Key label <input name='key_id' required></label>
<button type='submit'>Create API key</button>
</form>
{secret_box}
<table><thead><tr><th>Key label</th><th>Status</th><th>Created</th><th>Last used</th><th>Actions</th></tr></thead>
<tbody>{key_rows}</tbody></table>
"""
    return _page(request, "API keys", body, status_code=status_code)


@router.post("/ui/keys/create", response_class=HTMLResponse)
async def create_key(request: Request) -> HTMLResponse:
    if not get_ui_key_id(request):
        return RedirectResponse("/admin/login", status_code=303)
    data = await _form(request)
    try:
        created = container.api_keys.create(data.get("key_id", "").strip())
        return _keys_page(
            request,
            message="API key created. Copy it now; it will not be shown again.",
            new_key_label=created.key_id,
            new_key_secret=created.key,
        )
    except Exception as exc:  # noqa: BLE001
        return _keys_page(request, error=f"Unable to create API key: {exc}", status_code=400)


@router.post("/ui/keys/{key_id}/status")
async def key_status_action(request: Request, key_id: str) -> RedirectResponse:
    if not get_ui_key_id(request):
        return RedirectResponse("/admin/login", status_code=303)
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
def usage_page(request: Request) -> HTMLResponse:
    if not get_ui_key_id(request):
        return RedirectResponse("/admin/login", status_code=303)

    rows = "".join(
        f"<tr><td>{escape(e.timestamp)}</td><td>{escape(e.endpoint)}</td><td>{escape(e.method)}</td>"
        f"<td>{e.status_code}</td><td>{escape(e.api_key_id or e.subject)}</td>"
        f"<td>{escape(e.error_code or '-')}</td><td>{e.latency_ms}</td></tr>"
        for e in container.store.list_recent_usage_events(limit=100)
    )
    body = f"""
<h2>Recent activity</h2>
<table><thead><tr><th>Timestamp</th><th>Endpoint</th><th>Method</th><th>Status</th><th>Subject</th><th>Error</th><th>Latency (ms)</th></tr></thead>
<tbody>{rows}</tbody></table>
"""
    return _page(request, "Recent activity", body)
