"""Landing page routes (Sprint 28 + Sprint 29 i18n).

Serves ``GET /`` and ``GET /about`` with HTML pages that introduce
EGG-API to non-technical visitors. These pages are rendered server-side
from Jinja2 templates so no JavaScript is required — the same constraint
the operator console already lives under.

Sprint 29 plumbs :mod:`app.i18n` through so the same templates render in
English or French depending on the ``?lang`` query param, an
``egg_lang`` cookie, or the ``Accept-Language`` header. Selecting a
language via ``?lang=fr`` / ``?lang=en`` also writes the cookie so the
preference survives navigation.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import __version__
from app.dependencies import container
from app.i18n import LANG_COOKIE, SUPPORTED_LANGS, resolve_lang, translator

logger = logging.getLogger("egg.landing")


router = APIRouter(tags=["landing"])

_BRAND = "EGG-API"

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


def _liveness_status(t) -> dict[str, Any]:
    """Cheap live-check suitable for a public landing page.

    The backend probe is wrapped in a broad ``try/except`` because the
    landing page must render even when ES / OS is unreachable — the
    status tile simply shows a warning pill in that case.
    """

    status: dict[str, Any] = {
        "live_label": t("landing.status.online"),
        "live_class": "ok",
        "backend_label": t("landing.status.unknown"),
        "backend_class": "warn",
    }
    try:
        info = container.adapter.detect()
        version = info.get("version") if isinstance(info, dict) else None
        flavor = info.get("flavor") if isinstance(info, dict) else None
        if version or flavor:
            pretty = " ".join(p for p in (flavor, str(version or "")) if p)
            status["backend_label"] = pretty.strip() or t("landing.status.reachable")
            status["backend_class"] = "ok"
        else:
            status["backend_label"] = t("landing.status.reachable")
            status["backend_class"] = "ok"
    except Exception:
        logger.debug("landing_backend_probe_failed", exc_info=True)
        status["backend_label"] = t("landing.status.unreachable")
        status["backend_class"] = "warn"
    return status


def _render(template_name: str, request: Request) -> HTMLResponse:
    lang = resolve_lang(request)
    t = translator(lang)
    template = _env.get_template(template_name)
    html = template.render(
        brand=_BRAND,
        version=__version__,
        lang=lang,
        supported_langs=SUPPORTED_LANGS,
        t=t,
        status=_liveness_status(t),
    )
    response = HTMLResponse(content=html)
    # Persist the operator's language pick when they used ``?lang=``.
    q = (request.query_params.get("lang") or "").strip().lower()
    if q in SUPPORTED_LANGS:
        # One year, HttpOnly so JS can't read it. SameSite=lax keeps
        # the preference when navigating in from external links.
        response.set_cookie(
            LANG_COOKIE,
            q,
            max_age=365 * 24 * 3600,
            httponly=True,
            samesite="lax",
        )
    return response


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing_index(request: Request) -> HTMLResponse:
    return _render("index.html", request)


@router.get("/about", response_class=HTMLResponse, include_in_schema=False)
def landing_about(request: Request) -> HTMLResponse:
    return _render("about.html", request)
