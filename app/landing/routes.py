"""Landing page routes (Sprint 28).

Serves ``GET /`` and ``GET /about`` with HTML pages that introduce
EGG-API to non-technical visitors. These pages are rendered server-side
from Jinja2 templates so no JavaScript is required — the same constraint
the operator console already lives under.

The routes never touch authentication or database state; failure
handling stays cheap and deterministic. A tiny "status tile" fetches
live readiness *only* when the template asks for it (not on every
imported module) so landing never blocks startup or breaks when the
backend is briefly unreachable.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import __version__
from app.dependencies import container

logger = logging.getLogger("egg.landing")


router = APIRouter(tags=["landing"])

_BRAND = "EGG-API"

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


def _liveness_status() -> dict[str, Any]:
    """Cheap live-check suitable for a public landing page.

    The backend probe is wrapped in a broad ``try/except`` because the
    landing page must render even when ES / OS is unreachable — the
    status tile simply shows a warning pill in that case.
    """

    status: dict[str, Any] = {
        "live_label": "online",
        "live_class": "ok",
        "backend_label": "unknown",
        "backend_class": "warn",
    }
    try:
        info = container.adapter.detect()
        version = info.get("version") if isinstance(info, dict) else None
        flavor = info.get("flavor") if isinstance(info, dict) else None
        if version or flavor:
            pretty = " ".join(p for p in (flavor, str(version or "")) if p)
            status["backend_label"] = pretty.strip() or "reachable"
            status["backend_class"] = "ok"
        else:
            status["backend_label"] = "reachable"
            status["backend_class"] = "ok"
    except Exception:
        logger.debug("landing_backend_probe_failed", exc_info=True)
        status["backend_label"] = "unreachable"
        status["backend_class"] = "warn"
    return status


def _render(template_name: str) -> HTMLResponse:
    template = _env.get_template(template_name)
    html = template.render(
        brand=_BRAND,
        version=__version__,
        status=_liveness_status(),
    )
    return HTMLResponse(content=html)


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing_index() -> HTMLResponse:
    return _render("index.html")


@router.get("/about", response_class=HTMLResponse, include_in_schema=False)
def landing_about() -> HTMLResponse:
    return _render("about.html")
