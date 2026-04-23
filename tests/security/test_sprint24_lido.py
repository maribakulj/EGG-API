"""Sprint 24 regression tests: LIDO importer (OAI-PMH + flat file).

Covers:
- LIDO ``lido:lido`` element → backend-doc mapper (museum profile
  fields: inventory_number, artist, medium, dimensions,
  acquisition_date, current_location, iiif_manifest, thumbnail);
- Deleted / malformed / bare-lido envelope handling on the OAI path;
- Flat-file ingest: happy path + malformed XML + missing file;
- Dispatcher in :mod:`app.importers`: kind routing, unknown-kind
  guard, empty-URL guard;
- Admin REST surface now accepts ``oaipmh_lido`` / ``lido_file`` kinds,
  and ``/identify`` works for LIDO-over-OAI but refuses flat files;
- Admin UI kind selector renders the three options and ``/add``
  persists a flat-file source with pinned empty metadata prefix.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from app.importers import lido, run_import
from app.importers.lido import (
    ingest_file,
    lido_element_to_doc,
    oai_record_to_doc,
    parse_lido_bytes,
)

# ---------------------------------------------------------------------------
# LIDO XML factories
# ---------------------------------------------------------------------------


_LIDO_NS = 'xmlns:lido="http://www.lido-schema.org"'


def _lido_record(
    record_id: str,
    *,
    title: str = "La Joconde",
    work_type: str = "painting",
    artist: str = "Leonardo da Vinci",
    inv_no: str = "INV-779",
    medium: str = "oil on wood",
    dimensions: str = "77 x 53 cm",
    production_date: str = "circa 1503",
    acquisition_date: str = "1797",
    iiif: str | None = "https://example.org/iiif/mona-lisa/manifest",
    thumbnail: str | None = "https://example.org/thumb/mona-lisa.jpg",
    repository: str = "Louvre",
) -> str:
    iiif_block = (
        f'<lido:resourceRepresentation lido:type="IIIFManifest">'
        f"<lido:linkResource>{iiif}</lido:linkResource>"
        f"</lido:resourceRepresentation>"
        if iiif
        else ""
    )
    thumb_block = (
        f'<lido:resourceRepresentation lido:type="thumbnail">'
        f"<lido:linkResource>{thumbnail}</lido:linkResource>"
        f"</lido:resourceRepresentation>"
        if thumbnail
        else ""
    )
    return f"""
<lido:lido {_LIDO_NS}>
  <lido:lidoRecID lido:type="global">{record_id}</lido:lidoRecID>
  <lido:descriptiveMetadata xml:lang="en">
    <lido:objectClassificationWrap>
      <lido:objectWorkTypeWrap>
        <lido:objectWorkType>
          <lido:term>{work_type}</lido:term>
        </lido:objectWorkType>
      </lido:objectWorkTypeWrap>
    </lido:objectClassificationWrap>
    <lido:objectIdentificationWrap>
      <lido:titleWrap>
        <lido:titleSet>
          <lido:appellationValue>{title}</lido:appellationValue>
        </lido:titleSet>
      </lido:titleWrap>
      <lido:repositoryWrap>
        <lido:repositorySet>
          <lido:repositoryName>
            <lido:legalBodyName>
              <lido:appellationValue>{repository}</lido:appellationValue>
            </lido:legalBodyName>
          </lido:repositoryName>
          <lido:workID lido:type="inventory number">{inv_no}</lido:workID>
        </lido:repositorySet>
      </lido:repositoryWrap>
      <lido:objectMaterialsTechWrap>
        <lido:objectMaterialsTechSet>
          <lido:materialsTech>
            <lido:termMaterialsTech>
              <lido:term>{medium}</lido:term>
            </lido:termMaterialsTech>
          </lido:materialsTech>
        </lido:objectMaterialsTechSet>
      </lido:objectMaterialsTechWrap>
      <lido:objectMeasurementsWrap>
        <lido:objectMeasurementsSet>
          <lido:displayObjectMeasurements>{dimensions}</lido:displayObjectMeasurements>
        </lido:objectMeasurementsSet>
      </lido:objectMeasurementsWrap>
    </lido:objectIdentificationWrap>
    <lido:eventWrap>
      <lido:eventSet>
        <lido:event>
          <lido:eventType><lido:term>production</lido:term></lido:eventType>
          <lido:eventActor>
            <lido:actorInRole>
              <lido:actor>
                <lido:nameActorSet>
                  <lido:appellationValue>{artist}</lido:appellationValue>
                </lido:nameActorSet>
              </lido:actor>
            </lido:actorInRole>
          </lido:eventActor>
          <lido:eventDate>
            <lido:displayDate>{production_date}</lido:displayDate>
          </lido:eventDate>
        </lido:event>
        <lido:event>
          <lido:eventType><lido:term>acquisition</lido:term></lido:eventType>
          <lido:eventDate>
            <lido:displayDate>{acquisition_date}</lido:displayDate>
          </lido:eventDate>
        </lido:event>
      </lido:eventSet>
    </lido:eventWrap>
  </lido:descriptiveMetadata>
  <lido:administrativeMetadata xml:lang="en">
    <lido:resourceWrap>
      <lido:resourceSet>
        {iiif_block}
        {thumb_block}
      </lido:resourceSet>
    </lido:resourceWrap>
  </lido:administrativeMetadata>
</lido:lido>
"""


def _lido_wrap(records: list[str]) -> str:
    inner = "\n".join(records)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<lidoWrap {_LIDO_NS.replace("xmlns:lido", "xmlns")}
          xmlns:lido="http://www.lido-schema.org">
  {inner}
</lidoWrap>
"""


def _oai_lido_envelope(records: list[str], *, resumption_token: str = "") -> str:
    token_xml = f"<resumptionToken>{resumption_token}</resumptionToken>" if resumption_token else ""
    record_xml = "".join(
        f"<record><header><identifier>{i}</identifier></header>"
        f"<metadata><lido:lidoWrap {_LIDO_NS}>{rec}</lido:lidoWrap></metadata></record>"
        for i, rec in enumerate(records, start=1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-04-23T00:00:00Z</responseDate>
  <request verb="ListRecords">https://example.org/oai</request>
  <ListRecords>
    {record_xml}
    {token_xml}
  </ListRecords>
</OAI-PMH>
"""


def _mock_client(responses: list[str]) -> httpx.Client:
    iterator = iter(responses)

    def _handler(_request: httpx.Request) -> httpx.Response:
        body = next(iterator)
        return httpx.Response(200, content=body.encode("utf-8"))

    return httpx.Client(transport=httpx.MockTransport(_handler))


# ---------------------------------------------------------------------------
# Parser: element → doc
# ---------------------------------------------------------------------------


def test_lido_element_maps_all_museum_fields() -> None:
    el = ET.fromstring(_lido_record("EXMU-42"))
    doc = lido_element_to_doc(el)
    assert doc is not None
    assert doc["id"] == "EXMU-42"
    assert doc["type"] == "painting"
    assert doc["title"] == "La Joconde"
    assert doc["artist"] == "Leonardo da Vinci"
    assert doc["creators"] == ["Leonardo da Vinci"]
    assert doc["inventory_number"] == "INV-779"
    assert doc["medium"] == "oil on wood"
    assert doc["dimensions"] == "77 x 53 cm"
    assert doc["acquisition_date"] == "1797"
    assert doc["date"] == "circa 1503"  # production date fills general date
    assert doc["current_location"] == "Louvre"
    assert doc["iiif_manifest"] == "https://example.org/iiif/mona-lisa/manifest"
    assert doc["thumbnail"] == "https://example.org/thumb/mona-lisa.jpg"


def test_lido_element_without_id_returns_none() -> None:
    bad = f"<lido:lido {_LIDO_NS}></lido:lido>"
    assert lido_element_to_doc(ET.fromstring(bad)) is None


def test_lido_element_falls_back_to_recordid_when_no_lidorecid() -> None:
    xml = f"""
<lido:lido {_LIDO_NS}>
  <lido:administrativeMetadata>
    <lido:recordWrap>
      <lido:recordID lido:type="local">FALLBACK-1</lido:recordID>
    </lido:recordWrap>
  </lido:administrativeMetadata>
</lido:lido>
"""
    doc = lido_element_to_doc(ET.fromstring(xml))
    assert doc is not None
    assert doc["id"] == "FALLBACK-1"
    assert doc["type"] == "object"  # default when no work type


def test_lido_element_prefers_inventory_type_over_first_workid() -> None:
    xml = f"""
<lido:lido {_LIDO_NS}>
  <lido:lidoRecID>ID-1</lido:lidoRecID>
  <lido:descriptiveMetadata>
    <lido:objectIdentificationWrap>
      <lido:repositoryWrap>
        <lido:repositorySet>
          <lido:workID lido:type="catalog number">CAT-9</lido:workID>
          <lido:workID lido:type="inventory number">INV-42</lido:workID>
        </lido:repositorySet>
      </lido:repositoryWrap>
    </lido:objectIdentificationWrap>
  </lido:descriptiveMetadata>
</lido:lido>
"""
    doc = lido_element_to_doc(ET.fromstring(xml))
    assert doc is not None
    assert doc["inventory_number"] == "INV-42"


def test_lido_element_iiif_heuristic_on_link_suffix() -> None:
    xml = f"""
<lido:lido {_LIDO_NS}>
  <lido:lidoRecID>ID-1</lido:lidoRecID>
  <lido:administrativeMetadata>
    <lido:resourceWrap>
      <lido:resourceSet>
        <lido:resourceRepresentation lido:type="manifest">
          <lido:linkResource>https://cdn.example.org/obj/1/manifest</lido:linkResource>
        </lido:resourceRepresentation>
      </lido:resourceSet>
    </lido:resourceWrap>
  </lido:administrativeMetadata>
</lido:lido>
"""
    doc = lido_element_to_doc(ET.fromstring(xml))
    assert doc is not None
    # URL ends with /manifest so the heuristic keeps it even though the
    # @lido:type does not contain "iiif".
    assert doc["iiif_manifest"] == "https://cdn.example.org/obj/1/manifest"


# ---------------------------------------------------------------------------
# Flat-file parse + ingest
# ---------------------------------------------------------------------------


def test_parse_lido_bytes_yields_all_records_in_wrap() -> None:
    payload = _lido_wrap(
        [
            _lido_record("A", title="Alpha", iiif=None, thumbnail=None),
            _lido_record("B", title="Beta", iiif=None, thumbnail=None),
        ]
    ).encode("utf-8")
    docs = list(parse_lido_bytes(payload))
    assert [d["id"] for d in docs] == ["A", "B"]
    assert [d["title"] for d in docs] == ["Alpha", "Beta"]


def test_parse_lido_bytes_accepts_single_bare_lido_root() -> None:
    payload = _lido_record("SOLO").encode("utf-8")
    docs = list(parse_lido_bytes(payload))
    assert len(docs) == 1
    assert docs[0]["id"] == "SOLO"


def test_parse_lido_bytes_rejects_malformed_xml() -> None:
    from app.errors import AppError

    with pytest.raises(AppError) as exc_info:
        list(parse_lido_bytes(b"<not really xml"))
    assert exc_info.value.code == "backend_unavailable"


def test_ingest_file_streams_through_bulk_index(tmp_path: Path) -> None:
    path = tmp_path / "dump.xml"
    path.write_text(_lido_wrap([_lido_record("A"), _lido_record("B")]))

    seen: list[list[dict]] = []

    def _bulk(docs: list[dict]) -> tuple[int, int]:
        seen.append(list(docs))
        return len(docs), 0

    result = ingest_file(path=path, bulk_index=_bulk, chunk_size=1)
    assert result.ingested == 2
    assert result.failed == 0
    assert result.error is None
    # chunk_size=1 → two calls, each with one doc
    assert [len(chunk) for chunk in seen] == [1, 1]


def test_ingest_file_reports_missing_path(tmp_path: Path) -> None:
    result = ingest_file(path=tmp_path / "nope.xml", bulk_index=lambda _docs: (0, 0))
    assert result.ingested == 0
    assert result.error is not None
    assert "LIDO file not found" in result.error


def test_ingest_file_reports_malformed_xml(tmp_path: Path) -> None:
    path = tmp_path / "broken.xml"
    path.write_text("<not really xml")

    result = ingest_file(path=path, bulk_index=lambda _docs: (0, 0))
    assert result.ingested == 0
    assert result.error is not None
    assert "not valid XML" in result.error


# ---------------------------------------------------------------------------
# OAI-PMH LIDO path (reuses the S22 envelope)
# ---------------------------------------------------------------------------


def test_oai_record_to_doc_parses_lidowrap_envelope() -> None:
    record_xml = f"""
<record xmlns="http://www.openarchives.org/OAI/2.0/">
  <header><identifier>oai:ex:1</identifier></header>
  <metadata>
    <lido:lidoWrap {_LIDO_NS}>{_lido_record("REC-1")}</lido:lidoWrap>
  </metadata>
</record>
"""
    rec = ET.fromstring(record_xml)
    header = rec.find("{http://www.openarchives.org/OAI/2.0/}header")
    metadata = rec.find("{http://www.openarchives.org/OAI/2.0/}metadata")
    doc = oai_record_to_doc(header, metadata)
    assert doc is not None
    assert doc["id"] == "REC-1"
    assert doc["title"] == "La Joconde"


def test_oai_record_to_doc_skips_deleted_records() -> None:
    rec = ET.fromstring(
        '<record xmlns="http://www.openarchives.org/OAI/2.0/">'
        '<header status="deleted"><identifier>oai:ex:1</identifier></header>'
        "</record>"
    )
    header = rec.find("{http://www.openarchives.org/OAI/2.0/}header")
    assert oai_record_to_doc(header, None) is None


def test_oai_record_to_doc_returns_none_when_no_metadata() -> None:
    rec = ET.fromstring(
        '<record xmlns="http://www.openarchives.org/OAI/2.0/">'
        "<header><identifier>oai:ex:1</identifier></header>"
        "</record>"
    )
    header = rec.find("{http://www.openarchives.org/OAI/2.0/}header")
    assert oai_record_to_doc(header, None) is None


def test_oaipmh_ingest_with_lido_parser_streams_museum_docs() -> None:
    from app.importers.oaipmh import ingest as oai_ingest

    mock = _mock_client([_oai_lido_envelope([_lido_record("A"), _lido_record("B")])])
    seen: list[list[dict]] = []

    def _bulk(docs: list[dict]) -> tuple[int, int]:
        seen.append(list(docs))
        return len(docs), 0

    result = oai_ingest(
        url="https://example.org/oai",
        metadata_prefix="lido",
        bulk_index=_bulk,
        client=mock,
        record_parser=lido.oai_record_to_doc,
    )
    assert result.ingested == 2
    assert result.failed == 0
    assert result.error is None
    assert seen[0][0]["type"] == "painting"
    assert {d["id"] for d in seen[0]} == {"A", "B"}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class _FakeSource:
    def __init__(self, **kw):
        self.kind = kw.pop("kind", "oaipmh")
        self.url = kw.pop("url", None)
        self.metadata_prefix = kw.pop("metadata_prefix", None)
        self.set_spec = kw.pop("set_spec", None)


def test_dispatcher_routes_oaipmh_lido(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.importers import oaipmh as oai_mod

    calls: dict[str, object] = {}

    def _fake_ingest(**kw):
        calls.update(kw)
        return oai_mod.OAIImportResult(ingested=3, failed=0)

    monkeypatch.setattr("app.importers.oai_ingest", _fake_ingest)
    src = _FakeSource(kind="oaipmh_lido", url="https://ex/oai", metadata_prefix=None)
    result = run_import(src, bulk_index=lambda _docs: (0, 0))
    assert result.ingested == 3
    # Defaults to "lido" prefix and pins the LIDO parser.
    assert calls["metadata_prefix"] == "lido"
    assert calls["record_parser"] is lido.oai_record_to_doc


def test_dispatcher_routes_lido_file(tmp_path: Path) -> None:
    path = tmp_path / "dump.xml"
    path.write_text(_lido_wrap([_lido_record("Z")]))
    src = _FakeSource(kind="lido_file", url=str(path))
    seen = []

    def _bulk(docs):
        seen.extend(docs)
        return len(docs), 0

    result = run_import(src, bulk_index=_bulk)
    assert result.ingested == 1
    assert result.error is None
    assert seen[0]["id"] == "Z"


def test_dispatcher_rejects_empty_url() -> None:
    src = _FakeSource(kind="oaipmh_lido", url=None)
    result = run_import(src, bulk_index=lambda _d: (0, 0))
    assert result.error == "OAI-PMH (LIDO) source has no URL"


def test_dispatcher_rejects_unknown_kind() -> None:
    src = _FakeSource(kind="magic", url="x")
    with pytest.raises(ValueError, match="Unknown import source kind"):
        run_import(src, bulk_index=lambda _d: (0, 0))


# ---------------------------------------------------------------------------
# Admin REST surface
# ---------------------------------------------------------------------------


def test_imports_api_accepts_oaipmh_lido_kind(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/admin/v1/imports",
        json={
            "label": "Louvre OAI LIDO",
            "kind": "oaipmh_lido",
            "url": "https://louvre.example.org/oai",
            "metadata_prefix": "lido",
            "schema_profile": "museum",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "oaipmh_lido"
    assert body["schema_profile"] == "museum"


def test_imports_api_accepts_lido_file_kind(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/admin/v1/imports",
        json={
            "label": "Museum local dump",
            "kind": "lido_file",
            "url": "/tmp/does-not-need-to-exist-at-create.xml",
            "schema_profile": "museum",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == "lido_file"


def test_imports_api_rejects_unknown_kind(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/admin/v1/imports",
        json={"label": "x", "kind": "marc", "url": "http://x"},
        headers=admin_headers,
    )
    assert resp.status_code == 422  # Pydantic literal mismatch


def test_imports_api_run_lido_file_end_to_end(
    client: TestClient,
    admin_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    path = tmp_path / "lido.xml"
    path.write_text(_lido_wrap([_lido_record("END-1")]))
    created = client.post(
        "/admin/v1/imports",
        json={
            "label": "end-to-end",
            "kind": "lido_file",
            "url": str(path),
            "schema_profile": "museum",
        },
        headers=admin_headers,
    ).json()

    resp = client.post(f"/admin/v1/imports/{created['id']}/run", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["records_ingested"] == 1
    assert body["error"] is None


def test_imports_api_identify_refuses_lido_file(
    client: TestClient, admin_headers: dict[str, str], tmp_path: Path
) -> None:
    path = tmp_path / "lido.xml"
    path.write_text("<placeholder/>")
    created = client.post(
        "/admin/v1/imports",
        json={
            "label": "no-identify",
            "kind": "lido_file",
            "url": str(path),
        },
        headers=admin_headers,
    ).json()
    resp = client.post(f"/admin/v1/imports/{created['id']}/identify", headers=admin_headers)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------


def test_imports_ui_form_lists_new_kinds(client: TestClient, admin_ui_session: str) -> None:
    resp = client.get("/admin/ui/imports")
    assert resp.status_code == 200
    assert 'value="oaipmh"' in resp.text
    assert 'value="oaipmh_lido"' in resp.text
    assert 'value="lido_file"' in resp.text


def test_imports_ui_add_lido_file_persists_empty_prefix(
    client: TestClient, admin_ui_session: str, tmp_path: Path
) -> None:
    path = tmp_path / "lido.xml"
    path.write_text(_lido_wrap([_lido_record("UI-1")]))
    resp = client.post(
        "/admin/ui/imports/add",
        data={
            "csrf_token": admin_ui_session,
            "label": "museum-dump",
            "kind": "lido_file",
            "url": str(path),
            # metadata_prefix is ignored for lido_file; send a value anyway.
            "metadata_prefix": "lido",
            "schema_profile": "museum",
        },
    )
    assert resp.status_code == 200
    from app.dependencies import container

    sources = container.store.list_import_sources()
    assert any(
        s.kind == "lido_file" and s.schema_profile == "museum" and s.metadata_prefix in (None, "")
        for s in sources
    )


def test_imports_ui_add_rejects_unknown_kind(client: TestClient, admin_ui_session: str) -> None:
    resp = client.post(
        "/admin/ui/imports/add",
        data={
            "csrf_token": admin_ui_session,
            "label": "x",
            "kind": "not-a-kind",
            "url": "/tmp/x",
        },
    )
    assert resp.status_code == 400
    assert "Unknown importer kind" in resp.text
