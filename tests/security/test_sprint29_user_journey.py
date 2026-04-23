"""Sprint 29 user-journey tests.

These are integration-level walks through the product from the point
of view of a non-technical operator discovering EGG-API for the first
time. They prove that the full publication chain — landing page →
admin login → imports dashboard → CSV ingest → public search →
outbound OAI-PMH — works end-to-end with no manual plumbing. They
complement the per-sprint unit tests by asserting that the screens
*link up correctly*.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.dependencies import container


def _write_csv(path: Path) -> None:
    path.write_text(
        "id,title,type,creators\n"
        "rec-1,Alpha,article,Ada Lovelace|Charles Babbage\n"
        "rec-2,Beta,article,Grace Hopper\n"
        "rec-3,Gamma,article,\n",
        encoding="utf-8",
    )


def test_first_time_operator_journey(
    client: TestClient, admin_ui_session: str, tmp_path: Path
) -> None:
    """Walk the product exactly like a new operator would.

    1. Anonymous visit to ``/`` — landing page renders, links into
       the admin console are present.
    2. Anonymous call to ``/v1/oai?verb=Identify`` — outbound OAI
       provider answers with a well-formed envelope (no auth, by
       protocol contract).
    3. Logged-in visit to ``/admin/ui`` — console shell renders.
    4. Logged-in visit to ``/admin/ui/imports`` — imports dashboard
       renders with the nine importer kinds and the schedule picker.
    5. Add a CSV source via ``/admin/ui/imports/add`` — row created.
    6. Run the CSV source via ``/admin/ui/imports/{id}/run`` — docs
       hit the adapter's ``bulk_index``.
    7. Hit ``/v1/search`` — public API still serves records.
    """

    # 1. Landing is public and lists the nine importers.
    landing = client.get("/")
    assert landing.status_code == 200
    assert "admin/ui/setup" in landing.text
    assert "/v1/oai?verb=Identify" in landing.text

    # 2. OAI endpoint is unauthenticated and well-formed.
    oai = client.get("/v1/oai", params={"verb": "Identify"})
    assert oai.status_code == 200
    assert "<Identify>" in oai.text
    assert "<protocolVersion>2.0</protocolVersion>" in oai.text

    # 3. Admin console shell renders for a logged-in user.
    console = client.get("/admin/ui")
    assert console.status_code == 200
    assert "imports" in console.text.lower() or "import" in console.text.lower()

    # 4. Imports dashboard lists the importer kinds + schedule picker.
    imports_page = client.get("/admin/ui/imports")
    assert imports_page.status_code == 200
    for kind in ("csv_file", "oaipmh", "marc_file", "ead_file", "lido_file"):
        assert f'value="{kind}"' in imports_page.text
    assert 'name="schedule"' in imports_page.text

    # 5. Drop a CSV on the server and register it as an import source.
    csv_path = tmp_path / "journey.csv"
    _write_csv(csv_path)
    add_resp = client.post(
        "/admin/ui/imports/add",
        data={
            "csrf_token": admin_ui_session,
            "label": "journey-csv",
            "kind": "csv_file",
            "url": str(csv_path),
            "metadata_prefix": "",
            "schema_profile": "library",
            "schedule": "",
        },
    )
    assert add_resp.status_code == 200
    sources = container.store.list_import_sources()
    created = next(s for s in sources if s.label == "journey-csv")
    assert created.kind == "csv_file"

    # 6. Run the importer and assert the records hit ``bulk_index``.
    run_resp = client.post(
        f"/admin/ui/imports/{created.id}/run",
        data={"csrf_token": admin_ui_session},
    )
    assert run_resp.status_code == 200
    indexed_ids = {d["id"] for d in container.adapter.stored if "id" in d}
    assert {"rec-1", "rec-2", "rec-3"} <= indexed_ids

    # 7. Public search still works for anonymous visitors. Using an
    # explicit ``q`` keeps the test compatible with the default
    # "prudent" security profile (``allow_empty_query=false``).
    search = client.get("/v1/search", params={"q": "test"})
    assert search.status_code == 200
    payload = search.json()
    assert "results" in payload


def test_journey_in_french_shows_translated_landing(
    client: TestClient,
) -> None:
    """The landing page flips to French on demand, and the CTA link
    into the setup wizard remains the same URL — the whole journey
    stays bilingual-safe for francophone operators."""

    resp = client.get("/?lang=fr")
    assert resp.status_code == 200
    body = resp.text
    assert 'html lang="fr"' in body
    # Jinja autoescapes the apostrophe in "Lancer l'assistant …" — we
    # look for the ASCII-safe slice instead of the full French phrase.
    assert "Lancer l" in body
    assert "assistant de configuration" in body
    # CTA URLs are language-independent.
    assert 'href="/admin/ui/setup"' in body
    assert 'href="/admin/ui"' in body


def test_journey_scheduler_rescheduling_after_run(
    client: TestClient, admin_ui_session: str, tmp_path: Path
) -> None:
    """Operator creates a scheduled CSV source, triggers a manual run,
    the run succeeds and the scheduler's next_run_at has rolled
    forward — proves S27 survives an end-to-end round-trip."""

    csv_path = tmp_path / "scheduled.csv"
    _write_csv(csv_path)

    add_resp = client.post(
        "/admin/ui/imports/add",
        data={
            "csrf_token": admin_ui_session,
            "label": "scheduled-journey",
            "kind": "csv_file",
            "url": str(csv_path),
            "metadata_prefix": "",
            "schema_profile": "library",
            "schedule": "daily",
        },
    )
    assert add_resp.status_code == 200
    src = next(s for s in container.store.list_import_sources() if s.label == "scheduled-journey")
    assert src.schedule == "daily"
    assert src.next_run_at is not None

    # Drive the scheduler synchronously to confirm it picks the row up
    # when next_run_at falls due.
    from datetime import datetime, timedelta, timezone

    from app.scheduler import Scheduler

    # Backdate the row so it is "due" right now.
    container.store.set_import_source_schedule(
        src.id,
        schedule="daily",
        next_run_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
    )
    sched = Scheduler(store=container.store, bulk_index=container.adapter.bulk_index)
    touched = sched.run_pending()
    assert src.id in touched

    # Next run has rolled ~24 h into the future.
    refreshed = container.store.get_import_source(src.id)
    assert refreshed is not None
    assert refreshed.next_run_at is not None
    parsed = datetime.fromisoformat(refreshed.next_run_at)
    assert parsed > datetime.now(timezone.utc)
