"""Release-info endpoint (Sprint 19).

``GET /admin/v1/releases`` gives the admin dashboard (and the Briefcase
desktop launcher's tray menu, later) a cheap way to tell the operator
whether their installed version is current.

We don't bundle a signed auto-updater — Briefcase ships its own on
macOS/Windows and Linux AppImage users run their own updater chain.
All we do here is:

- report the running version (``app.__version__``) and the running
  platform;
- best-effort poll GitHub's releases API for the latest tag;
- never block the request on GitHub (5-second timeout + TTL cache).

The poll honours ``EGG_RELEASES_REPO`` (defaults to
``maribakulj/egg-api``) and can be disabled entirely with
``EGG_DISABLE_RELEASE_CHECK=1`` for air-gapped deployments.
"""

from __future__ import annotations

import os
import platform
import threading
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends

from app import __version__
from app.auth.dependencies import require_admin_key
from app.logging import get_logger

logger = get_logger("egg.releases")

router = APIRouter(
    prefix="/admin/v1",
    tags=["admin", "releases"],
    dependencies=[Depends(require_admin_key)],
)


_CACHE_TTL_SECONDS = 600
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"expires_at": 0.0, "payload": None}


def _default_repo() -> str:
    return os.getenv("EGG_RELEASES_REPO", "maribakulj/egg-api").strip()


def _check_disabled() -> bool:
    return os.getenv("EGG_DISABLE_RELEASE_CHECK", "").strip().lower() in {"1", "true", "yes"}


def _fetch_latest_from_github(
    repo: str, *, client: httpx.Client | None = None
) -> dict[str, Any] | None:
    """Best-effort poll. Returns ``None`` on any kind of failure."""
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json"}
    close_after = False
    if client is None:
        client = httpx.Client(timeout=5.0, follow_redirects=False)
        close_after = True
    try:
        resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        logger.info("release_check_failed_soft", exc_info=True)
        return None
    finally:
        if close_after:
            client.close()
    tag = (data.get("tag_name") or "").lstrip("v")
    if not tag:
        return None
    return {
        "latest_version": tag,
        "html_url": data.get("html_url"),
        "published_at": data.get("published_at"),
        "assets": [
            {
                "name": asset.get("name"),
                "browser_download_url": asset.get("browser_download_url"),
                "size": asset.get("size"),
            }
            for asset in (data.get("assets") or [])
        ],
    }


def _build_payload(client: httpx.Client | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "current_version": __version__,
        "platform": platform.system().lower(),
        "python": platform.python_version(),
        "latest_version": None,
        "update_available": False,
        "release_check_disabled": _check_disabled(),
        "repository": _default_repo(),
    }
    if payload["release_check_disabled"]:
        return payload
    remote = _fetch_latest_from_github(_default_repo(), client=client)
    if remote is None:
        return payload
    payload.update(remote)
    payload["update_available"] = remote["latest_version"] != __version__
    return payload


def _get_payload_cached(client: httpx.Client | None = None) -> dict[str, Any]:
    now = time.monotonic()
    with _cache_lock:
        if _cache["payload"] is not None and _cache["expires_at"] > now:
            return dict(_cache["payload"])
    payload = _build_payload(client=client)
    with _cache_lock:
        _cache["payload"] = payload
        _cache["expires_at"] = now + _CACHE_TTL_SECONDS
    return dict(payload)


def _invalidate_cache() -> None:
    """Test hook: drop any cached payload so the next call re-fetches."""
    with _cache_lock:
        _cache["payload"] = None
        _cache["expires_at"] = 0.0


@router.get("/releases")
def releases() -> dict[str, Any]:
    """Return the installed version + best-effort latest release info.

    Always responds within ~5s even when GitHub is unreachable; a
    failed upstream check surfaces as ``latest_version: null``. The
    endpoint is admin-gated to avoid an anonymous caller triggering
    external requests.
    """
    return _get_payload_cached()
