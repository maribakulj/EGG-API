"""Sprint 16 regression tests: first-run UX (OTP + CLI + error helper).

Covers:

- ``SQLiteStore.create_setup_otp`` / ``consume_setup_otp``: single
  use, hashed at rest, expires after TTL;
- ``GET /admin/setup-otp/{token}`` exchanges a valid OTP for a fresh
  admin UI session, bounces to the wizard, and refuses
  expired/consumed tokens with a login page fallback;
- ``translate_app_error`` / ``format_for_terminal`` produce
  human-readable messages for the common AppError codes and for
  Pydantic ValidationError payloads.

The ``egg-api start`` command itself is not executed end-to-end (that
would launch uvicorn); we unit-test its helpers through the store and
the user-errors module.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.config.models import AppConfig
from app.dependencies import container
from app.errors import AppError
from app.user_errors import (
    format_for_terminal,
    translate_app_error,
    translate_validation_error,
)

# ---------------------------------------------------------------------------
# Store: OTP lifecycle
# ---------------------------------------------------------------------------


def test_otp_round_trip_consumes_once() -> None:
    token = container.store.create_setup_otp("admin", ttl_seconds=60)
    assert token
    assert container.store.consume_setup_otp(token) == "admin"
    # Second redemption must fail — OTPs are single-use.
    assert container.store.consume_setup_otp(token) is None


def test_otp_rejects_unknown_token() -> None:
    assert container.store.consume_setup_otp("not-a-real-token") is None
    assert container.store.consume_setup_otp(None) is None
    assert container.store.consume_setup_otp("") is None


def test_otp_respects_ttl() -> None:
    # Clamped at 30s by create_setup_otp; sleep past the window.
    token = container.store.create_setup_otp("admin", ttl_seconds=1)
    # Force expiry by rewriting the stored row.
    with container.store._connect() as conn:
        conn.execute(
            "UPDATE setup_otps SET expires_at = ? WHERE token_hash = ?",
            ("1970-01-01T00:00:00+00:00", _hash(token)),
        )
    assert container.store.consume_setup_otp(token) is None


def test_otp_purge_cleans_expired_and_consumed() -> None:
    container.store.purge_expired_setup_otps()  # clean slate
    _used = container.store.create_setup_otp("admin", ttl_seconds=60)
    _expired = container.store.create_setup_otp("admin", ttl_seconds=60)
    assert container.store.consume_setup_otp(_used) == "admin"
    with container.store._connect() as conn:
        conn.execute(
            "UPDATE setup_otps SET expires_at = ? WHERE token_hash = ?",
            ("1970-01-01T00:00:00+00:00", _hash(_expired)),
        )
    removed = container.store.purge_expired_setup_otps()
    assert removed >= 2


def _hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()


# ---------------------------------------------------------------------------
# HTTP: /admin/setup-otp/{token}
# ---------------------------------------------------------------------------


def test_magic_link_issues_session_and_redirects(client: TestClient) -> None:
    token = container.store.create_setup_otp("admin", ttl_seconds=60)
    resp = client.get(f"/admin/setup-otp/{token}", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin/ui/setup"
    # The session cookie is set so the wizard loads without a login.
    assert "egg_admin_session" in resp.headers.get("set-cookie", "")


def test_magic_link_for_unknown_token_renders_login(client: TestClient) -> None:
    resp = client.get("/admin/setup-otp/nope-nope-nope", follow_redirects=False)
    assert resp.status_code == 401
    assert "one-time link" in resp.text.lower() or "expired" in resp.text.lower()


def test_magic_link_is_single_use(client: TestClient) -> None:
    token = container.store.create_setup_otp("admin", ttl_seconds=60)
    ok = client.get(f"/admin/setup-otp/{token}", follow_redirects=False)
    assert ok.status_code == 303
    again = client.get(f"/admin/setup-otp/{token}", follow_redirects=False)
    assert again.status_code == 401


# ---------------------------------------------------------------------------
# user_errors
# ---------------------------------------------------------------------------


def test_translate_app_error_matches_specific_field_hint() -> None:
    exc = AppError(
        "invalid_parameter",
        "Key label is not valid",
        {"key_id": "bad label!"},
        status_code=400,
    )
    out = translate_app_error(exc)
    assert out["code"] == "invalid_parameter"
    assert "label" in out["user_message"].lower()
    assert "1-64" in out["suggestion"]


def test_translate_app_error_falls_back_to_code_hint() -> None:
    exc = AppError(
        "backend_unavailable",
        "raw",
        {"reason": "timeout"},
        status_code=503,
    )
    out = translate_app_error(exc)
    assert "search backend" in out["user_message"].lower()
    assert "elasticsearch" in out["suggestion"].lower() or "opensearch" in out["suggestion"].lower()


def test_translate_app_error_generic_fallback() -> None:
    exc = AppError("made_up_code", "boom", {}, status_code=500)
    out = translate_app_error(exc)
    # No hint match: still gives a usable sentence + suggestion.
    assert out["user_message"]
    assert out["suggestion"]


def test_format_for_terminal_handles_pydantic_validation() -> None:
    with pytest.raises(ValidationError) as info:
        AppConfig.model_validate({"backend": {"url": 42}})  # type wrong
    msg = format_for_terminal(info.value)
    assert "problems" in msg
    assert "backend" in msg


def test_format_for_terminal_handles_app_error() -> None:
    exc = AppError("forbidden", "denied", {}, status_code=403)
    msg = format_for_terminal(exc)
    assert "permission" in msg.lower() or "admin" in msg.lower()
    assert "(code: forbidden)" in msg


def test_translate_validation_error_enumerates_fields() -> None:
    with pytest.raises(ValidationError) as info:
        AppConfig.model_validate({"backend": {"url": 42}})
    out = translate_validation_error(info.value)
    assert "backend" in out
    assert out.startswith("Configuration has the following problems:")


# ---------------------------------------------------------------------------
# CLI helpers (the start command itself is not spawned — it would hand off
# control to uvicorn. We test the pieces it composes.)
# ---------------------------------------------------------------------------


def test_schedule_browser_open_fires_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import cli

    calls: list[str] = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: calls.append(url))
    cli._schedule_browser_open("http://127.0.0.1:9/setup", delay_seconds=0.01)
    # Background thread; give it a moment.
    time.sleep(0.1)
    assert calls == ["http://127.0.0.1:9/setup"]


def test_start_subparser_is_registered() -> None:
    from app import cli

    parser = cli.build_parser()
    # argparse raises SystemExit on unknown subcommands, so a successful
    # parse of ``start --no-browser`` proves the subparser is wired.
    ns = parser.parse_args(["start", "--no-browser", "--port", "65000"])
    assert ns.command == "start"
    assert ns.no_browser is True
    assert ns.port == 65000
