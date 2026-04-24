"""Sprint 26 regression tests: EAD importer (OAI-PMH + flat file).

Covers:
- EAD 2002 (no namespace) and EAD3 (archivists.org schema) element
  → backend-doc mapping;
- archdesc + multi-level ``<c>`` component hierarchy, parent_id
  pointers, <unitdate normal="…"> preference over text;
- Flat-file ingest (happy, missing file, malformed XML) streaming
  through bulk_index with chunking;
- OAI-PMH path where one OAI record expands to many backend docs;
- Dispatcher routing for ``ead_file`` + ``oaipmh_ead``;
- ArchiveFields sub-model keeps archive out of library/museum wire;
- Mapper with ``schema_profile=archive`` emits nested
  ``archive.*`` sub-block via dotted mapping keys;
- propose_mapping() for archive suggests the new archive.* hints;
- Setup wizard mapping template lists the archive.* slots when
  profile=archive is selected;
- Admin REST + UI accept the two new kinds.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient

from app.importers import run_import
from app.importers.ead import (
    ingest_file,
    iter_ead_docs,
    oai_record_to_docs,
    parse_ead_bytes,
)

# ---------------------------------------------------------------------------
# EAD XML factories — cover both the 2002 (no namespace) and EAD3 shapes
# ---------------------------------------------------------------------------


def _ead_2002(with_components: bool = True) -> bytes:
    components = (
        """
        <dsc>
          <c01 level="series">
            <did>
              <unitid>S-001</unitid>
              <unittitle>Correspondance administrative</unittitle>
              <unitdate normal="1920/1935">1920-1935</unitdate>
            </did>
            <scopecontent><p>Lettres reçues et envoyées.</p></scopecontent>
            <c02 level="file">
              <did>
                <unitid>F-001</unitid>
                <unittitle>Registre 1920</unittitle>
                <unitdate>1920</unitdate>
              </did>
            </c02>
          </c01>
        </dsc>
        """
        if with_components
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ead>
  <eadheader><eadid>findingaid-42</eadid></eadheader>
  <archdesc level="fonds">
    <did>
      <unitid>FR-ARCH-42</unitid>
      <unittitle>Fonds Durand</unittitle>
      <unitdate normal="1890/1960">1890-1960</unitdate>
      <origination>Durand, Jean</origination>
      <repository>Archives municipales de Lyon</repository>
      <physdesc><extent>2,5 mètres linéaires</extent></physdesc>
    </did>
    <scopecontent>
      <p>Papiers privés et documents professionnels.</p>
      <p>Second paragraphe de description.</p>
    </scopecontent>
    <accessrestrict><p>Consultation libre.</p></accessrestrict>
    {components}
  </archdesc>
</ead>
""".encode()


def _ead3(with_components: bool = True) -> bytes:
    ns = 'xmlns="http://ead3.archivists.org/schema/"'
    components = (
        """
        <dsc>
          <c level="file">
            <did>
              <unitid>EAD3-F-1</unitid>
              <unittitle>Cahier 1</unittitle>
              <unitdate normal="1902">1902</unitdate>
            </did>
          </c>
        </dsc>
        """
        if with_components
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ead {ns}>
  <control><recordid>ead3-example</recordid></control>
  <archdesc level="collection">
    <did>
      <unitid>EAD3-COL-1</unitid>
      <unittitle>Collection Martin</unittitle>
      <repository>Archives nationales</repository>
    </did>
    <scopecontent><p>Notes personnelles.</p></scopecontent>
    {components}
  </archdesc>
</ead>
""".encode()


def _oai_ead_envelope(ead_bodies: list[bytes]) -> str:
    records_xml = "".join(
        f"<record><header><identifier>oai:ex:{i}</identifier></header>"
        f"<metadata>{body.decode('utf-8').split('?>')[1]}</metadata></record>"
        for i, body in enumerate(ead_bodies, start=1)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">
  <responseDate>2026-04-23T00:00:00Z</responseDate>
  <request verb="ListRecords">https://example.org/oai</request>
  <ListRecords>{records_xml}</ListRecords>
</OAI-PMH>
"""


def _mock_client(responses: list[str]) -> httpx.Client:
    iterator = iter(responses)

    def _handler(_request: httpx.Request) -> httpx.Response:
        body = next(iterator)
        return httpx.Response(200, content=body.encode("utf-8"))

    return httpx.Client(transport=httpx.MockTransport(_handler))


# ---------------------------------------------------------------------------
# Parser: EAD 2002
# ---------------------------------------------------------------------------


def test_ead2002_archdesc_plus_components() -> None:
    root = ET.fromstring(_ead_2002())
    docs = list(iter_ead_docs(root))
    assert len(docs) == 3  # archdesc + c01 + c02
    root_doc, series_doc, file_doc = docs
    assert root_doc["id"] == "FR-ARCH-42"
    assert root_doc["type"] == "fonds"
    assert root_doc["title"] == "Fonds Durand"
    assert root_doc["creators"] == ["Durand, Jean"]
    assert "Papiers privés" in root_doc["description"]
    assert "Second paragraphe" in root_doc["description"]
    assert root_doc["repository"] == "Archives municipales de Lyon"
    assert root_doc["extent"] == "2,5 mètres linéaires"
    assert root_doc["access_conditions"] == "Consultation libre."
    assert root_doc["date"] == "1890"  # @normal start wins over text
    assert root_doc["unit_level"] == "fonds"

    assert series_doc["id"] == "S-001"
    assert series_doc["type"] == "series"
    assert series_doc["parent_id"] == "FR-ARCH-42"
    assert series_doc["date"] == "1920"

    assert file_doc["id"] == "F-001"
    assert file_doc["parent_id"] == "S-001"
    assert file_doc["date"] == "1920"


def test_ead3_namespaced_parse() -> None:
    docs = list(parse_ead_bytes(_ead3()))
    assert len(docs) == 2
    root_doc, file_doc = docs
    assert root_doc["id"] == "EAD3-COL-1"
    assert root_doc["type"] == "collection"
    assert root_doc["title"] == "Collection Martin"
    assert root_doc["repository"] == "Archives nationales"
    assert file_doc["id"] == "EAD3-F-1"
    assert file_doc["parent_id"] == "EAD3-COL-1"
    assert file_doc["date"] == "1902"


def test_ead_parse_handles_archdesc_only_payload() -> None:
    # OAI-PMH often wraps only <archdesc>, not <ead>.
    payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<archdesc level="fonds">
  <did>
    <unitid>BARE-1</unitid>
    <unittitle>Solo</unittitle>
  </did>
</archdesc>"""
    docs = list(parse_ead_bytes(payload))
    assert len(docs) == 1
    assert docs[0]["id"] == "BARE-1"
    assert docs[0]["type"] == "fonds"


def test_ead_parse_malformed_xml_raises() -> None:
    from app.errors import AppError

    with pytest.raises(AppError):
        list(parse_ead_bytes(b"<not really ead"))


def test_ead_parse_empty_when_no_archdesc() -> None:
    # Root with no archdesc inside → no docs, no exception.
    payload = b"<?xml version='1.0'?><random><hello/></random>"
    assert list(parse_ead_bytes(payload)) == []


# ---------------------------------------------------------------------------
# Flat-file ingest
# ---------------------------------------------------------------------------


def test_ingest_file_streams_through_bulk_index(tmp_path: Path) -> None:
    path = tmp_path / "fa.xml"
    path.write_bytes(_ead_2002())
    seen: list[list[dict]] = []

    def _bulk(docs):
        seen.append(list(docs))
        return len(docs), 0

    result = ingest_file(path=path, bulk_index=_bulk, chunk_size=2)
    assert result.ingested == 3
    assert result.failed == 0
    assert result.error is None
    assert [len(c) for c in seen] == [2, 1]


def test_ingest_file_missing(tmp_path: Path) -> None:
    result = ingest_file(path=tmp_path / "nope.xml", bulk_index=lambda _d: (0, 0))
    assert result.ingested == 0
    assert result.error is not None
    assert "EAD file not found" in result.error


def test_ingest_file_malformed(tmp_path: Path) -> None:
    path = tmp_path / "broken.xml"
    path.write_text("<not really xml")
    result = ingest_file(path=path, bulk_index=lambda _d: (0, 0))
    assert result.ingested == 0
    assert result.error is not None
    assert "not valid XML" in result.error


# ---------------------------------------------------------------------------
# OAI-PMH path
# ---------------------------------------------------------------------------


def test_oai_record_to_docs_expands_one_oai_record_into_tree() -> None:
    record_xml = f"""
<record xmlns="http://www.openarchives.org/OAI/2.0/">
  <header><identifier>oai:ex:1</identifier></header>
  <metadata>{_ead_2002().decode("utf-8").split("?>")[1]}</metadata>
</record>
"""
    rec = ET.fromstring(record_xml)
    header = rec.find("{http://www.openarchives.org/OAI/2.0/}header")
    metadata = rec.find("{http://www.openarchives.org/OAI/2.0/}metadata")
    docs = oai_record_to_docs(header, metadata)
    assert len(docs) == 3
    assert [d["id"] for d in docs] == ["FR-ARCH-42", "S-001", "F-001"]


def test_oai_record_to_docs_skips_deleted() -> None:
    rec = ET.fromstring(
        '<record xmlns="http://www.openarchives.org/OAI/2.0/">'
        '<header status="deleted"><identifier>oai:ex:1</identifier></header>'
        "</record>"
    )
    header = rec.find("{http://www.openarchives.org/OAI/2.0/}header")
    assert oai_record_to_docs(header, None) == []


def test_oaipmh_ead_ingest_streams_all_docs() -> None:
    from app.importers.ead import oai_record_to_docs
    from app.importers.oaipmh import ingest as oai_ingest

    envelope = _oai_ead_envelope([_ead_2002()])
    mock = _mock_client([envelope])
    seen: list[dict] = []

    def _bulk(docs):
        seen.extend(docs)
        return len(docs), 0

    result = oai_ingest(
        url="https://example.org/oai",
        metadata_prefix="ead",
        bulk_index=_bulk,
        client=mock,
        record_parser=oai_record_to_docs,
    )
    # One OAI record → 3 EAD docs (archdesc + 2 components).
    assert result.ingested == 3
    assert {d["id"] for d in seen} == {"FR-ARCH-42", "S-001", "F-001"}


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class _FakeSource:
    def __init__(self, **kw):
        self.kind = kw.pop("kind", "oaipmh")
        self.url = kw.pop("url", None)
        self.metadata_prefix = kw.pop("metadata_prefix", None)
        self.set_spec = kw.pop("set_spec", None)


def test_dispatcher_runs_ead_file(tmp_path: Path) -> None:
    path = tmp_path / "fa.xml"
    path.write_bytes(_ead_2002())
    src = _FakeSource(kind="ead_file", url=str(path))
    seen: list[dict] = []

    def _bulk(docs):
        seen.extend(docs)
        return len(docs), 0

    result = run_import(src, bulk_index=_bulk)
    assert result.ingested == 3
    assert {d["id"] for d in seen} == {"FR-ARCH-42", "S-001", "F-001"}


def test_dispatcher_rejects_ead_file_missing_url() -> None:
    src = _FakeSource(kind="ead_file", url=None)
    result = run_import(src, bulk_index=lambda _d: (0, 0))
    assert result.error == "EAD file source has no path"


def test_dispatcher_rejects_oaipmh_ead_missing_url() -> None:
    src = _FakeSource(kind="oaipmh_ead", url=None)
    result = run_import(src, bulk_index=lambda _d: (0, 0))
    assert result.error == "OAI-PMH (EAD) source has no URL"


# ---------------------------------------------------------------------------
# Archive profile integration — ArchiveFields on the wire shape
# ---------------------------------------------------------------------------


def _archive_config():
    from app.config.models import AppConfig

    return AppConfig.model_validate(
        {
            "schema_profile": "archive",
            "mapping": {
                "id": {"source": "unit_id", "mode": "direct", "criticality": "required"},
                "type": {"source": "unit_level", "mode": "direct", "criticality": "required"},
                "title": {"source": "title", "mode": "direct"},
                "archive.unit_id": {"source": "unit_id", "mode": "direct"},
                "archive.unit_level": {"source": "unit_level", "mode": "direct"},
                "archive.extent": {"source": "extent", "mode": "direct"},
                "archive.repository": {"source": "repository", "mode": "direct"},
                "archive.scope_content": {"source": "scope_content", "mode": "direct"},
                "archive.access_conditions": {
                    "source": "access_conditions",
                    "mode": "direct",
                },
                "archive.parent_id": {"source": "parent_id", "mode": "direct"},
            },
            "allowed_include_fields": ["id", "type", "title", "archive"],
        }
    )


def test_mapper_emits_archive_sub_block_when_profile_is_archive() -> None:
    from app.mappers.schema_mapper import SchemaMapper

    mapper = SchemaMapper(_archive_config())
    record = mapper.map_record(
        {
            "unit_id": "ARCH-1",
            "unit_level": "fonds",
            "title": "Fonds Test",
            "repository": "Archives X",
            "scope_content": "Papiers.",
            "access_conditions": "Libre.",
            "extent": "10 ml",
        }
    )
    assert record.archive is not None
    assert record.archive.unit_id == "ARCH-1"
    assert record.archive.unit_level == "fonds"
    assert record.archive.repository == "Archives X"
    assert record.archive.scope_content == "Papiers."
    assert record.archive.access_conditions == "Libre."
    assert record.archive.extent == "10 ml"


def test_library_config_does_not_emit_archive_block() -> None:
    from app.config.models import AppConfig
    from app.mappers.schema_mapper import SchemaMapper

    mapper = SchemaMapper(AppConfig())
    record = mapper.map_record({"id": "1", "type": "book", "title": "Library item"})
    assert record.archive is None


def test_library_record_has_no_archive_block() -> None:
    from app.schemas.record import Record

    rec = Record(id="x", type="book", title="Hello")
    assert rec.archive is None


def test_app_config_allows_archive_head_in_include_fields() -> None:
    from app.config.models import AppConfig

    cfg = AppConfig.model_validate(
        {
            "mapping": {
                "id": {"source": "id", "mode": "direct", "criticality": "required"},
                "type": {"source": "type", "mode": "direct", "criticality": "required"},
                "archive.unit_id": {"source": "unitid", "mode": "direct"},
            },
            "allowed_include_fields": ["id", "type", "archive"],
        }
    )
    assert "archive" in cfg.allowed_include_fields


# ---------------------------------------------------------------------------
# Setup wizard — hints + public_fields for archive profile
# ---------------------------------------------------------------------------


def test_propose_mapping_archive_includes_archive_fields() -> None:
    from app.admin_ui.setup_service import propose_mapping

    available = {
        "unitid": "text",
        "unittitle": "text",
        "scopecontent": "text",
        "extent": "text",
        "repository": "text",
        "level": "keyword",
        "origination": "text",
    }
    mapping = propose_mapping(available, profile="archive")
    assert mapping["id"]["source"] == "unitid"
    assert mapping["title"]["source"] == "unittitle"
    assert mapping["archive.unit_id"]["source"] == "unitid"
    assert mapping["archive.unit_level"]["source"] == "level"
    assert mapping["archive.extent"]["source"] == "extent"
    assert mapping["archive.repository"]["source"] == "repository"
    assert mapping["archive.scope_content"]["source"] == "scopecontent"


def test_wizard_mapping_template_lists_archive_fields(
    client: TestClient, admin_ui_session: str
) -> None:
    from app.dependencies import container

    container.store.save_setup_draft(
        "admin",
        {
            "backend": {"type": "elasticsearch", "url": "http://x", "auth": {"mode": "none"}},
            "source": {"index": "records"},
            "available_fields": {
                "unitid": "text",
                "unittitle": "text",
                "extent": "text",
                "level": "keyword",
                "scopecontent": "text",
                "repository": "text",
            },
            "available_indices": ["records"],
            "mapping": {},
            "schema_profile": "archive",
            "detected_version": None,
        },
        "mapping",
    )

    resp = client.get("/admin/ui/setup/mapping")
    assert resp.status_code == 200
    assert "archive.unit_id" in resp.text
    assert "archive.repository" in resp.text
    assert "archive.scope_content" in resp.text


# ---------------------------------------------------------------------------
# Admin REST + UI surface
# ---------------------------------------------------------------------------


def test_imports_api_accepts_ead_file(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/admin/v1/imports",
        json={
            "label": "Archives Lyon",
            "kind": "ead_file",
            "url": "/tmp/findingaid.xml",
            "schema_profile": "archive",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == "ead_file"


def test_imports_api_accepts_oaipmh_ead(client: TestClient, admin_headers: dict[str, str]) -> None:
    resp = client.post(
        "/admin/v1/imports",
        json={
            "label": "AtoM OAI",
            "kind": "oaipmh_ead",
            "url": "https://atom.example.org/oai",
            "schema_profile": "archive",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201


def test_imports_api_run_ead_file_end_to_end(
    client: TestClient, admin_headers: dict[str, str], tmp_path: Path
) -> None:
    path = tmp_path / "fa.xml"
    path.write_bytes(_ead_2002())
    created = client.post(
        "/admin/v1/imports",
        json={
            "label": "e2e",
            "kind": "ead_file",
            "url": str(path),
            "schema_profile": "archive",
        },
        headers=admin_headers,
    ).json()
    resp = client.post(f"/admin/v1/imports/{created['id']}/run", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["records_ingested"] == 3


def test_imports_api_identify_works_for_oaipmh_ead(
    client: TestClient, admin_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    created = client.post(
        "/admin/v1/imports",
        json={
            "label": "x",
            "kind": "oaipmh_ead",
            "url": "https://example.org/oai",
        },
        headers=admin_headers,
    ).json()

    def _fake_identify(url: str):
        return {"repository_name": "Fake"}

    from app.admin_api import imports as imports_mod

    monkeypatch.setattr(imports_mod, "oai_identify", _fake_identify)
    resp = client.post(f"/admin/v1/imports/{created['id']}/identify", headers=admin_headers)
    assert resp.status_code == 200


def test_imports_ui_form_lists_ead_options(client: TestClient, admin_ui_session: str) -> None:
    resp = client.get("/admin/ui/imports")
    assert resp.status_code == 200
    assert 'value="oaipmh_ead"' in resp.text
    assert 'value="ead_file"' in resp.text


def test_imports_ui_add_ead_file(client: TestClient, admin_ui_session: str, tmp_path: Path) -> None:
    path = tmp_path / "fa.xml"
    path.write_bytes(_ead_2002(with_components=False))
    resp = client.post(
        "/admin/ui/imports/add",
        data={
            "csrf_token": admin_ui_session,
            "label": "ui-ead",
            "kind": "ead_file",
            "url": str(path),
            "schema_profile": "archive",
        },
    )
    assert resp.status_code == 200
    from app.dependencies import container

    assert any(
        s.kind == "ead_file" and s.schema_profile == "archive"
        for s in container.store.list_import_sources()
    )
