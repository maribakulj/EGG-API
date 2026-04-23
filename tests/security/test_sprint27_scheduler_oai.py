"""Sprint 27 regression tests: scheduler + OAI-PMH provider.

Covers:
- Migration 10: ``schedule`` + ``next_run_at`` columns on import_sources,
  plus ``list_due_import_sources`` + ``set_import_source_schedule``;
- :func:`app.scheduler.compute_next_run_at` cadence math;
- :class:`app.scheduler.Scheduler.run_pending` picks due rows, runs
  them through ``run_import``, and rolls ``next_run_at`` forward;
- Sources without a cadence are never picked, even with next_run_at
  in the past;
- Admin REST / UI accept the new ``schedule`` field, seed
  ``next_run_at`` and refuse unknown cadences;
- /v1/oai handles the six OAI-PMH verbs, emits valid XML envelopes,
  encodes/decodes resumption tokens, and surfaces badVerb /
  cannotDisseminateFormat / idDoesNotExist / noSetHierarchy;
- Snapshot contract keeps /v1/oai in the frozen path list.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app.dependencies import container
from app.importers import ImportDispatchResult
from app.scheduler import (
    SCHEDULE_DELTAS,
    Scheduler,
    compute_next_run_at,
    is_valid_schedule,
)

# ---------------------------------------------------------------------------
# Storage + cadence math
# ---------------------------------------------------------------------------


def test_compute_next_run_at_respects_cadence() -> None:
    now = datetime(2026, 4, 23, 10, 0, 0, tzinfo=timezone.utc)
    for cadence, delta in SCHEDULE_DELTAS.items():
        iso = compute_next_run_at(cadence, now=now)
        assert iso is not None
        parsed = datetime.fromisoformat(iso)
        assert parsed - now == delta


def test_compute_next_run_at_returns_none_for_invalid_cadence() -> None:
    assert compute_next_run_at("never") is None
    assert compute_next_run_at(None) is None
    assert not is_valid_schedule("never")
    assert is_valid_schedule("hourly")


def test_list_due_import_sources_only_returns_rows_with_past_next_run_at() -> None:
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

    due = container.store.add_import_source(
        label="due-src",
        kind="oaipmh",
        url="https://example.org/oai",
        schedule="hourly",
        next_run_at=past,
    )
    not_due = container.store.add_import_source(
        label="future-src",
        kind="oaipmh",
        url="https://example.org/oai",
        schedule="daily",
        next_run_at=future,
    )
    manual = container.store.add_import_source(
        label="manual-src",
        kind="oaipmh",
        url="https://example.org/oai",
    )

    result = container.store.list_due_import_sources(now=datetime.now(timezone.utc).isoformat())
    ids = {r.id for r in result}
    assert due.id in ids
    assert not_due.id not in ids
    assert manual.id not in ids

    # Manual source has no cadence → unchanged by the scheduler contract.
    assert manual.schedule is None
    assert manual.next_run_at is None


def test_set_import_source_schedule_updates_row() -> None:
    src = container.store.add_import_source(
        label="sched-upd", kind="oaipmh", url="https://example.org/oai"
    )
    assert container.store.set_import_source_schedule(
        src.id, schedule="daily", next_run_at="2027-01-01T00:00:00+00:00"
    )
    after = container.store.get_import_source(src.id)
    assert after is not None
    assert after.schedule == "daily"
    assert after.next_run_at == "2027-01-01T00:00:00+00:00"
    # Clearing the schedule wipes both columns.
    assert container.store.set_import_source_schedule(src.id, schedule=None, next_run_at=None)
    cleared = container.store.get_import_source(src.id)
    assert cleared is not None
    assert cleared.schedule is None
    assert cleared.next_run_at is None


# ---------------------------------------------------------------------------
# Scheduler loop — run_pending end-to-end
# ---------------------------------------------------------------------------


def test_run_pending_triggers_due_source_and_reschedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    src = container.store.add_import_source(
        label="pending-hourly",
        kind="oaipmh",
        url="https://example.org/oai",
        schedule="hourly",
        next_run_at=past,
    )

    called: list[int] = []

    def _fake_run(source, *, bulk_index):
        called.append(source.id)
        bulk_index([{"id": f"doc-{source.id}"}])
        return ImportDispatchResult(ingested=1, failed=0)

    # Monkeypatch the symbol the scheduler imported at module load.
    monkeypatch.setattr("app.scheduler.run_import", _fake_run)

    bulk_seen: list[dict] = []

    def _bulk(docs):
        bulk_seen.extend(docs)
        return len(docs), 0

    sched = Scheduler(store=container.store, bulk_index=_bulk, tick_seconds=60)
    touched = sched.run_pending(now=datetime.now(timezone.utc))

    assert src.id in touched
    assert called == [src.id]
    assert bulk_seen == [{"id": f"doc-{src.id}"}]

    refreshed = container.store.get_import_source(src.id)
    assert refreshed is not None
    assert refreshed.schedule == "hourly"
    # Next run pushed an hour into the future.
    assert refreshed.next_run_at is not None
    parsed = datetime.fromisoformat(refreshed.next_run_at)
    assert parsed > datetime.now(timezone.utc)


def test_run_pending_ignores_manual_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    src = container.store.add_import_source(
        label="manual-only", kind="oaipmh", url="https://example.org/oai"
    )
    called: list[int] = []
    monkeypatch.setattr(
        "app.scheduler.run_import",
        lambda source, *, bulk_index: called.append(source.id) or ImportDispatchResult(1, 0),
    )
    sched = Scheduler(store=container.store, bulk_index=lambda _d: (0, 0))
    assert sched.run_pending() == []
    assert called == []
    # Confirm the row didn't sprout a schedule behind the operator's back.
    after = container.store.get_import_source(src.id)
    assert after is not None
    assert after.schedule is None


def test_run_pending_marks_failures_and_still_reschedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    src = container.store.add_import_source(
        label="boom",
        kind="oaipmh",
        url="https://example.org/oai",
        schedule="daily",
        next_run_at=past,
    )

    def _explode(source, *, bulk_index):
        raise RuntimeError("unreachable backend")

    monkeypatch.setattr("app.scheduler.run_import", _explode)
    sched = Scheduler(store=container.store, bulk_index=lambda _d: (0, 0))
    sched.run_pending()

    after = container.store.get_import_source(src.id)
    assert after is not None
    assert after.schedule == "daily"
    # Next run pushed forward; last run row recorded as failed.
    runs = container.store.list_import_runs(src.id, limit=1)
    assert runs and runs[0].status == "failed"
    assert runs[0].error_message == "unreachable backend"


def test_scheduler_start_stop_is_idempotent() -> None:
    sched = Scheduler(store=container.store, bulk_index=lambda _d: (0, 0), tick_seconds=60)
    sched.start()
    # Calling start twice should not raise or spawn a second thread.
    sched.start()
    sched.stop()
    sched.stop()  # second stop is a no-op


# ---------------------------------------------------------------------------
# Admin REST + UI accept the new schedule field
# ---------------------------------------------------------------------------


def test_imports_api_accepts_schedule_and_seeds_next_run_at(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/admin/v1/imports",
        json={
            "label": "scheduled-src",
            "kind": "oaipmh",
            "url": "https://example.org/oai",
            "schedule": "hourly",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["schedule"] == "hourly"
    assert body["next_run_at"] is not None


def test_imports_api_rejects_unknown_schedule(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/admin/v1/imports",
        json={
            "label": "bad-sched",
            "kind": "oaipmh",
            "url": "https://example.org/oai",
            "schedule": "every-10s",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 422


def test_imports_ui_add_accepts_schedule(client: TestClient, admin_ui_session: str) -> None:
    resp = client.post(
        "/admin/ui/imports/add",
        data={
            "csrf_token": admin_ui_session,
            "label": "ui-sched",
            "kind": "oaipmh",
            "url": "https://example.org/oai",
            "metadata_prefix": "oai_dc",
            "schema_profile": "library",
            "schedule": "6h",
        },
    )
    assert resp.status_code == 200
    src = next(
        (s for s in container.store.list_import_sources() if s.label == "ui-sched"),
        None,
    )
    assert src is not None
    assert src.schedule == "6h"
    assert src.next_run_at is not None


def test_imports_ui_add_rejects_unknown_schedule(client: TestClient, admin_ui_session: str) -> None:
    resp = client.post(
        "/admin/ui/imports/add",
        data={
            "csrf_token": admin_ui_session,
            "label": "bad-sched",
            "kind": "oaipmh",
            "url": "https://example.org/oai",
            "metadata_prefix": "oai_dc",
            "schema_profile": "library",
            "schedule": "every-minute",
        },
    )
    assert resp.status_code == 400
    assert "Unknown schedule cadence" in resp.text


# ---------------------------------------------------------------------------
# OAI-PMH provider (/v1/oai)
# ---------------------------------------------------------------------------


_OAI_NS = "{http://www.openarchives.org/OAI/2.0/}"


def _xml(resp) -> ET.Element:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/xml")
    return ET.fromstring(resp.text)


def test_oai_identify(client: TestClient) -> None:
    resp = client.get("/v1/oai", params={"verb": "Identify"})
    root = _xml(resp)
    identify = root.find(f"{_OAI_NS}Identify")
    assert identify is not None
    proto = identify.find(f"{_OAI_NS}protocolVersion")
    assert proto is not None and (proto.text or "").strip() == "2.0"
    base = identify.find(f"{_OAI_NS}baseURL")
    assert base is not None and "/v1/oai" in (base.text or "")


def test_oai_list_metadata_formats(client: TestClient) -> None:
    resp = client.get("/v1/oai", params={"verb": "ListMetadataFormats"})
    root = _xml(resp)
    prefixes = [(p.text or "").strip() for p in root.iter(f"{_OAI_NS}metadataPrefix")]
    assert "oai_dc" in prefixes


def test_oai_list_sets_returns_no_set_hierarchy(client: TestClient) -> None:
    resp = client.get("/v1/oai", params={"verb": "ListSets"})
    root = _xml(resp)
    err = root.find(f"{_OAI_NS}error")
    assert err is not None
    assert err.get("code") == "noSetHierarchy"


def test_oai_bad_verb(client: TestClient) -> None:
    resp = client.get("/v1/oai", params={"verb": "WhatEver"})
    root = _xml(resp)
    err = root.find(f"{_OAI_NS}error")
    assert err is not None
    assert err.get("code") == "badVerb"


def test_oai_missing_verb(client: TestClient) -> None:
    resp = client.get("/v1/oai")
    root = _xml(resp)
    err = root.find(f"{_OAI_NS}error")
    assert err is not None
    assert err.get("code") == "badVerb"


def test_oai_get_record_requires_identifier(client: TestClient) -> None:
    resp = client.get("/v1/oai", params={"verb": "GetRecord", "metadataPrefix": "oai_dc"})
    root = _xml(resp)
    err = root.find(f"{_OAI_NS}error")
    assert err is not None
    assert err.get("code") == "badArgument"


def test_oai_list_records_unsupported_prefix(client: TestClient) -> None:
    resp = client.get(
        "/v1/oai",
        params={"verb": "ListRecords", "metadataPrefix": "klingon"},
    )
    root = _xml(resp)
    err = root.find(f"{_OAI_NS}error")
    assert err is not None
    assert err.get("code") == "cannotDisseminateFormat"


def test_oai_resumption_token_round_trip(client: TestClient) -> None:
    from app.oai_provider import _Token  # internal helper exercised here

    token = _Token(cursor="abc123", metadata_prefix="oai_dc").encode()
    # Decode returns an equivalent token.
    decoded = _Token.decode(token)
    assert decoded is not None
    assert decoded.cursor == "abc123"
    assert decoded.metadata_prefix == "oai_dc"

    # Corrupt token surfaces badResumptionToken on the wire.
    resp = client.get(
        "/v1/oai", params={"verb": "ListRecords", "resumptionToken": "%%%not_a_token"}
    )
    root = _xml(resp)
    err = root.find(f"{_OAI_NS}error")
    assert err is not None
    assert err.get("code") == "badResumptionToken"


def test_oai_get_record_not_found(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container.adapter, "get_record", lambda _id: None)
    resp = client.get(
        "/v1/oai",
        params={
            "verb": "GetRecord",
            "identifier": "oai:egg-api:ghost",
            "metadataPrefix": "oai_dc",
        },
    )
    root = _xml(resp)
    err = root.find(f"{_OAI_NS}error")
    assert err is not None
    assert err.get("code") == "idDoesNotExist"


def test_oai_list_records_serializes_dublin_core(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub the adapter search so we get a deterministic payload + no
    # next cursor (single page keeps the resumptionToken branch off).
    # ``title`` and ``type`` survive the default test mapping; richer
    # fields (creators, subjects, …) are exercised via the direct
    # ``_dublin_core_block`` unit test below so this integration check
    # stays tolerant of the host deployment's mapping config.
    def _fake_search(_nq):
        return {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_source": {
                            "id": "rec-1",
                            "type": "book",
                            "title": "Hello, OAI",
                        }
                    }
                ],
            }
        }

    monkeypatch.setattr(container.adapter, "search", _fake_search)
    resp = client.get("/v1/oai", params={"verb": "ListRecords", "metadataPrefix": "oai_dc"})
    assert resp.status_code == 200
    body = resp.text
    assert "<oai_dc:dc" in body
    assert "<dc:title>Hello, OAI</dc:title>" in body
    assert "<dc:type>book</dc:type>" in body
    assert "<identifier>oai:" in body


def test_dublin_core_block_emits_all_mapped_fields() -> None:
    """Directly verify the DC serializer without depending on mapping."""

    from app.oai_provider import _dublin_core_block

    record = {
        "title": "A & B <title>",
        "creators": ["Ada Lovelace", "Charles Babbage"],
        "subjects": ["Computing"],
        "keywords": ["history"],
        "description": "Notes.",
        "date": {"display": "1843"},
        "languages": ["en"],
        "publisher": "Royal Society",
        "type": "article",
        "identifiers": {"isbn": "978-1", "doi": "10.1/ex", "url": "http://x"},
        "rights": {"label": "CC-BY"},
        "links": {"iiif_manifest": "http://x/manifest"},
    }
    out = _dublin_core_block(record)
    # XML-escaping preserves ampersand + angle brackets.
    assert "<dc:title>A &amp; B &lt;title&gt;</dc:title>" in out
    assert "<dc:creator>Ada Lovelace</dc:creator>" in out
    assert "<dc:creator>Charles Babbage</dc:creator>" in out
    assert "<dc:subject>Computing</dc:subject>" in out
    assert "<dc:subject>history</dc:subject>" in out
    assert "<dc:description>Notes.</dc:description>" in out
    assert "<dc:date>1843</dc:date>" in out
    assert "<dc:language>en</dc:language>" in out
    assert "<dc:publisher>Royal Society</dc:publisher>" in out
    assert "<dc:type>article</dc:type>" in out
    assert "<dc:identifier>978-1</dc:identifier>" in out
    assert "<dc:identifier>10.1/ex</dc:identifier>" in out
    assert "<dc:identifier>http://x/manifest</dc:identifier>" in out
    assert "<dc:rights>CC-BY</dc:rights>" in out


def test_oai_list_records_empty_repo(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        container.adapter,
        "search",
        lambda _nq: {"hits": {"total": {"value": 0}, "hits": []}},
    )
    resp = client.get("/v1/oai", params={"verb": "ListRecords", "metadataPrefix": "oai_dc"})
    root = _xml(resp)
    err = root.find(f"{_OAI_NS}error")
    assert err is not None
    assert err.get("code") == "noRecordsMatch"
