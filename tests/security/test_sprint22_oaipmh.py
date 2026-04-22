"""Sprint 22 regression tests: OAI-PMH Dublin Core importer.

Covers:
- OAI-PMH client (Identify, ListRecords) with resumption tokens,
  OAI-level error responses, XML parse failures, and the
  max-pages safety ceiling;
- Dublin Core → backend-doc mapping (titles, creators list,
  deleted-record skip, IIIF manifest heuristic);
- SQLiteStore CRUD for import_sources + import_runs;
- /admin/v1/imports CRUD + /run with a stubbed bulk_index;
- admin UI /admin/ui/imports page + add/run/delete actions.

Everything goes through httpx.MockTransport — no network traffic.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from app.dependencies import container
from app.importers import oaipmh

# ---------------------------------------------------------------------------
# Fixtures: OAI-PMH response factories
# ---------------------------------------------------------------------------


def _dc_record_xml(identifier: str, *, title: str = "", creators=(), deleted: bool = False) -> str:
    if deleted:
        return f"""
<record>
  <header status="deleted"><identifier>{identifier}</identifier></header>
</record>
"""
    creator_xml = "".join(f"<dc:creator>{c}</dc:creator>" for c in creators)
    return f"""
<record>
  <header><identifier>{identifier}</identifier></header>
  <metadata>
    <oai_dc:dc
      xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
      xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:identifier>{identifier}</dc:identifier>
      <dc:title>{title}</dc:title>
      {creator_xml}
      <dc:type>Book</dc:type>
    </oai_dc:dc>
  </metadata>
</record>
"""


def _list_records_xml(records: list[str], resumption_token: str = "") -> str:
    token_xml = f"<resumptionToken>{resumption_token}</resumptionToken>" if resumption_token else ""
    body = "\n".join(records)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-04-22T00:00:00Z</responseDate>
  <request verb="ListRecords">https://example.org/oai</request>
  <ListRecords>
    {body}
    {token_xml}
  </ListRecords>
</OAI-PMH>
"""


def _identify_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-04-22T00:00:00Z</responseDate>
  <request verb="Identify">https://example.org/oai</request>
  <Identify>
    <repositoryName>Example Library</repositoryName>
    <baseURL>https://example.org/oai</baseURL>
    <protocolVersion>2.0</protocolVersion>
    <earliestDatestamp>2000-01-01</earliestDatestamp>
    <granularity>YYYY-MM-DDThh:mm:ssZ</granularity>
  </Identify>
</OAI-PMH>
"""


def _error_xml(code: str, msg: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-04-22T00:00:00Z</responseDate>
  <request>https://example.org/oai</request>
  <error code="{code}">{msg}</error>
</OAI-PMH>
"""


def _mock_client(responses: list[str]) -> httpx.Client:
    """Return an httpx.Client that replays ``responses`` in order."""
    iterator = iter(responses)

    def _handler(_request: httpx.Request) -> httpx.Response:
        try:
            body = next(iterator)
        except StopIteration:  # pragma: no cover - defensive
            body = _list_records_xml([])
        return httpx.Response(200, content=body.encode("utf-8"))

    return httpx.Client(transport=httpx.MockTransport(_handler))


# ---------------------------------------------------------------------------
# Dublin Core mapping
# ---------------------------------------------------------------------------


def test_dc_record_to_doc_extracts_core_fields() -> None:
    xml = _dc_record_xml(
        "oai:example.org:1",
        title="Ulysses",
        creators=["Joyce, James", "Eliot, T. S."],
    )
    root = ET.fromstring(xml)
    header = root.find("header")
    metadata = root.find("metadata")
    doc = oaipmh.dc_record_to_doc(header, metadata)
    assert doc is not None
    assert doc["id"] == "oai:example.org:1"
    assert doc["title"] == "Ulysses"
    assert doc["creators"] == ["Joyce, James", "Eliot, T. S."]
    assert doc["type"] == "Book"


def test_dc_record_to_doc_detects_iiif_manifest_in_identifier() -> None:
    xml = """
<record xmlns:oai_dc="http://www.openarchives.org/OAI/2.0/oai_dc/"
        xmlns:dc="http://purl.org/dc/elements/1.1/">
  <header><identifier>oai:museum:42</identifier></header>
  <metadata>
    <oai_dc:dc>
      <dc:identifier>oai:museum:42</dc:identifier>
      <dc:identifier>https://iiif.example.org/iiif/42/manifest</dc:identifier>
      <dc:title>The Starry Night</dc:title>
    </oai_dc:dc>
  </metadata>
</record>
"""
    root = ET.fromstring(xml)
    doc = oaipmh.dc_record_to_doc(root.find("header"), root.find("metadata"))
    assert doc is not None
    assert doc["iiif_manifest"] == "https://iiif.example.org/iiif/42/manifest"


def test_dc_record_to_doc_skips_deleted_records() -> None:
    xml = _dc_record_xml("oai:example.org:deleted", deleted=True)
    root = ET.fromstring(xml)
    header = root.find("header")
    metadata = root.find("metadata")
    assert oaipmh.dc_record_to_doc(header, metadata) is None


# ---------------------------------------------------------------------------
# OAI-PMH client — identify + iter_records + resumption
# ---------------------------------------------------------------------------


def test_identify_returns_repo_summary() -> None:
    client = _mock_client([_identify_xml()])
    info = oaipmh.identify("https://example.org/oai", client=client)
    client.close()
    assert info["repository_name"] == "Example Library"
    assert info["protocol_version"] == "2.0"


def test_iter_records_follows_resumption_token() -> None:
    page1 = _list_records_xml(
        [_dc_record_xml("a", title="A"), _dc_record_xml("b", title="B")],
        resumption_token="cursor-2",
    )
    page2 = _list_records_xml([_dc_record_xml("c", title="C")])
    client = _mock_client([page1, page2])
    docs = list(oaipmh.iter_records("https://example.org/oai", client=client))
    client.close()
    assert [d["id"] for d in docs] == ["a", "b", "c"]


def test_iter_records_raises_on_oai_level_error() -> None:
    client = _mock_client([_error_xml("badVerb", "Illegal verb")])
    try:
        with pytest.raises(Exception) as info:
            list(oaipmh.iter_records("https://example.org/oai", client=client))
        assert "badVerb" in str(info.value)
    finally:
        client.close()


def test_iter_records_raises_on_http_error() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    try:
        with pytest.raises(Exception):
            list(oaipmh.iter_records("https://example.org/oai", client=client))
    finally:
        client.close()


def test_iter_records_raises_on_malformed_xml() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<not>valid<xml>")

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    try:
        with pytest.raises(Exception):
            list(oaipmh.iter_records("https://example.org/oai", client=client))
    finally:
        client.close()


def test_iter_records_max_pages_ceiling() -> None:
    loop_page = _list_records_xml([_dc_record_xml("loop", title="loop")], resumption_token="same")

    call_count = {"n": 0}

    def _handler(_request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(200, content=loop_page.encode("utf-8"))

    client = httpx.Client(transport=httpx.MockTransport(_handler))
    try:
        with pytest.raises(Exception) as info:
            list(oaipmh.iter_records("https://example.org/oai", client=client, max_pages=3))
        assert "max_pages" in str(info.value.args) or "3" in str(info.value)
    finally:
        client.close()
    assert call_count["n"] == 3


# ---------------------------------------------------------------------------
# ingest(): chunked bulk_index
# ---------------------------------------------------------------------------


def test_ingest_chunks_docs_through_bulk_index() -> None:
    # 7 records, chunk_size=3 → 3 calls (3, 3, 1).
    page = _list_records_xml([_dc_record_xml(f"r{i}", title=f"t{i}") for i in range(7)])
    client = _mock_client([page])

    batches: list[list[dict]] = []

    def _bulk(docs):
        batches.append(list(docs))
        return len(docs), 0

    try:
        result = oaipmh.ingest(
            url="https://example.org/oai",
            bulk_index=_bulk,
            chunk_size=3,
            client=client,
        )
    finally:
        client.close()
    assert result.ingested == 7
    assert result.failed == 0
    assert result.error is None
    assert [len(b) for b in batches] == [3, 3, 1]


def test_ingest_captures_error_without_raising() -> None:
    def _handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"oops")

    client = httpx.Client(transport=httpx.MockTransport(_handler))

    def _bulk(docs):
        return len(docs), 0

    try:
        result = oaipmh.ingest(url="https://example.org/oai", bulk_index=_bulk, client=client)
    finally:
        client.close()
    assert result.error is not None
    assert result.ingested == 0


# ---------------------------------------------------------------------------
# Store — import_sources + runs
# ---------------------------------------------------------------------------


def test_store_import_source_crud_and_runs() -> None:
    src = container.store.add_import_source(
        label="Koha test",
        kind="oaipmh",
        url="https://example.org/oai",
        metadata_prefix="oai_dc",
        set_spec="books",
    )
    assert src.id > 0
    loaded = container.store.get_import_source(src.id)
    assert loaded is not None
    assert loaded.label == "Koha test"

    run_id = container.store.start_import_run(src.id)
    container.store.finish_import_run(
        run_id, status="succeeded", records_ingested=42, records_failed=0
    )
    runs = container.store.list_import_runs(src.id)
    assert runs and runs[0].status == "succeeded"
    assert runs[0].records_ingested == 42

    assert container.store.delete_import_source(src.id) is True
    assert container.store.get_import_source(src.id) is None


# ---------------------------------------------------------------------------
# /admin/v1/imports — REST endpoints
# ---------------------------------------------------------------------------


def test_imports_api_requires_admin(client: TestClient) -> None:
    resp = client.get("/admin/v1/imports")
    assert resp.status_code == 401


def test_imports_api_create_list_delete(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/admin/v1/imports",
        json={
            "label": "My catalogue",
            "kind": "oaipmh",
            "url": "https://example.org/oai",
            "metadata_prefix": "oai_dc",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    source_id = resp.json()["id"]

    listed = client.get("/admin/v1/imports", headers=admin_headers).json()
    assert any(s["id"] == source_id for s in listed)

    resp = client.delete(f"/admin/v1/imports/{source_id}", headers=admin_headers)
    assert resp.status_code == 204


def test_imports_api_run_happy_path(
    client: TestClient, admin_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    created = client.post(
        "/admin/v1/imports",
        json={"label": "run-test", "kind": "oaipmh", "url": "https://example.org/oai"},
        headers=admin_headers,
    ).json()
    source_id = created["id"]

    # Stub the importer to avoid any HTTP call.
    from app.admin_api import imports as imports_mod

    def _fake_ingest(**kwargs):
        from app.importers.oaipmh import OAIImportResult

        # Use the passed-in bulk_index so we can assert it's wired.
        kwargs["bulk_index"]([{"id": "stub-1", "title": "stub"}])
        return OAIImportResult(ingested=1, failed=0)

    monkeypatch.setattr(imports_mod, "oai_ingest", _fake_ingest)

    resp = client.post(f"/admin/v1/imports/{source_id}/run", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["records_ingested"] == 1

    runs = client.get(f"/admin/v1/imports/{source_id}/runs", headers=admin_headers).json()
    assert runs and runs[0]["status"] == "succeeded"


def test_imports_api_run_records_failure(
    client: TestClient, admin_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    created = client.post(
        "/admin/v1/imports",
        json={"label": "fail-test", "kind": "oaipmh", "url": "https://example.org/oai"},
        headers=admin_headers,
    ).json()
    source_id = created["id"]

    from app.admin_api import imports as imports_mod

    def _fake_ingest(**kwargs):
        from app.importers.oaipmh import OAIImportResult

        return OAIImportResult(ingested=0, failed=0, error="unreachable")

    monkeypatch.setattr(imports_mod, "oai_ingest", _fake_ingest)
    resp = client.post(f"/admin/v1/imports/{source_id}/run", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["error"] == "unreachable"


def test_imports_api_delete_unknown_returns_404(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    resp = client.delete("/admin/v1/imports/999999", headers=admin_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# /admin/ui/imports — admin HTML
# ---------------------------------------------------------------------------


def test_imports_ui_requires_login(client: TestClient) -> None:
    resp = client.get("/admin/ui/imports", follow_redirects=False)
    assert resp.status_code == 303


def test_imports_ui_renders_add_form(client: TestClient, admin_ui_session: str) -> None:
    resp = client.get("/admin/ui/imports")
    assert resp.status_code == 200
    assert "Add an OAI-PMH source" in resp.text


def test_imports_ui_add_and_delete(client: TestClient, admin_ui_session: str) -> None:
    resp = client.post(
        "/admin/ui/imports/add",
        data={
            "csrf_token": admin_ui_session,
            "label": "UI source",
            "url": "https://example.org/oai",
            "metadata_prefix": "oai_dc",
            "schema_profile": "library",
        },
    )
    assert resp.status_code == 200
    assert "Added source: UI source" in resp.text
    sources = container.store.list_import_sources()
    created = next(s for s in sources if s.label == "UI source")

    resp = client.post(
        f"/admin/ui/imports/{created.id}/delete",
        data={"csrf_token": admin_ui_session},
    )
    assert resp.status_code == 200
    assert "Source removed." in resp.text


# ---------------------------------------------------------------------------
# End-to-end: OAI pages → FakeAdapter.stored
# ---------------------------------------------------------------------------


def test_ingest_end_to_end_stores_records_in_fake_adapter() -> None:
    page = _list_records_xml(
        [
            _dc_record_xml("e2e-1", title="E2E one"),
            _dc_record_xml("e2e-2", title="E2E two"),
        ]
    )
    client = _mock_client([page])
    adapter = container.adapter
    # Drop any previous state the FakeAdapter accumulated.
    adapter.stored = []  # type: ignore[attr-defined]
    try:
        result = oaipmh.ingest(
            url="https://example.org/oai",
            bulk_index=adapter.bulk_index,
            client=client,
        )
    finally:
        client.close()
    assert result.ingested == 2
    assert {d["id"] for d in adapter.stored} == {"e2e-1", "e2e-2"}  # type: ignore[attr-defined]
