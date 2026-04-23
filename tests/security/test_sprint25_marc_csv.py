"""Sprint 25 regression tests: MARC / UNIMARC + CSV importers.

Covers:
- ISO 2709 low-level parser (leader, directory, control/data fields,
  subfields) on both happy-path and malformed inputs;
- MARC21 and UNIMARC tag→doc mapping (title/author/publisher/date/
  isbn/subject fallbacks, creators from 100+700 vs 700+701);
- MARCXML parser + bare-``<record>`` fallback;
- Flat-file ingest for ``.mrc`` + MARCXML: happy path, missing file,
  malformed XML;
- CSV importer: header detection, semicolon dialect, list-splitting
  on ``|`` for plural fields, rejection of files without ``id``;
- Dispatcher: routing, flavor pinning via ``metadata_prefix``;
- Admin REST surface: new kinds validated, end-to-end run for each;
- Admin UI: form lists the seven kinds, MARC flat file persists
  flavor on the row.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.importers import run_import
from app.importers.csv_importer import ingest_csv_file, parse_csv_bytes
from app.importers.marc import (
    MarcField,
    MarcRecord,
    ingest_marc_file,
    ingest_marcxml_file,
    iter_iso2709_records,
    iter_marcxml_records,
    marc_record_to_doc,
    parse_iso2709_record,
)

# ---------------------------------------------------------------------------
# Binary MARC fixture helpers
# ---------------------------------------------------------------------------


def _encode_marc_record(leader_template: str, fields: list[tuple]) -> bytes:
    """Build a valid ISO 2709 record blob.

    ``fields`` entries are either
    ``(tag, True, value)`` for a control field, or
    ``(tag, False, (ind1, ind2, [(code, value), ...]))`` for data.
    """

    data_chunks: list[bytes] = []
    directory_entries: list[tuple[str, int, int]] = []
    base_offset = 0
    for tag, is_control, payload in fields:
        if is_control:
            chunk = payload.encode("utf-8") + b"\x1e"
        else:
            ind1, ind2, subs = payload
            body = b""
            for code, value in subs:
                body += b"\x1f" + (code + value).encode("utf-8")
            chunk = (ind1 + ind2).encode("utf-8") + body + b"\x1e"
        directory_entries.append((tag, len(chunk), base_offset))
        base_offset += len(chunk)
        data_chunks.append(chunk)

    directory_bytes = (
        b"".join(
            f"{tag:0>3}{length:04d}{offset:05d}".encode()
            for tag, length, offset in directory_entries
        )
        + b"\x1e"
    )
    data_bytes = b"".join(data_chunks)
    base_address = 24 + len(directory_bytes)
    total = base_address + len(data_bytes) + 1  # trailing 0x1D
    leader = f"{total:05d}" + leader_template[5:12] + f"{base_address:05d}" + leader_template[17:]
    return leader.encode("utf-8") + directory_bytes + data_bytes + b"\x1d"


_LEADER21 = "00000cam a2200000 a 4500"


def _marc21_book() -> bytes:
    return _encode_marc_record(
        _LEADER21,
        [
            ("001", True, "BK-0001"),
            ("100", False, ("1", " ", [("a", "Doe, Jane")])),
            (
                "245",
                False,
                (
                    "1",
                    "0",
                    [
                        ("a", "A Tale of Two Libraries :"),
                        ("b", "the definitive edition"),
                        ("c", "Jane Doe"),
                    ],
                ),
            ),
            ("260", False, (" ", " ", [("b", "ExPress,"), ("c", "2023.")])),
            ("700", False, ("1", " ", [("a", "Smith, John")])),
            ("020", False, (" ", " ", [("a", "9781234567890 (pbk)")])),
            ("650", False, (" ", "0", [("a", "Libraries--History.")])),
            ("500", False, (" ", " ", [("a", "Includes index.")])),
        ],
    )


def _unimarc_book() -> bytes:
    return _encode_marc_record(
        _LEADER21,
        [
            ("001", True, "UNI-0001"),
            (
                "200",
                False,
                (
                    "1",
                    " ",
                    [
                        ("a", "Histoire des bibliothèques"),
                        ("e", "du Moyen Âge à nos jours"),
                    ],
                ),
            ),
            ("210", False, (" ", " ", [("c", "ExPresse"), ("d", "2023")])),
            ("700", False, (" ", " ", [("a", "Durand, Marie")])),
            ("701", False, (" ", " ", [("a", "Martin, Pierre")])),
            ("010", False, (" ", " ", [("a", "9782345678901")])),
            ("606", False, (" ", " ", [("a", "Bibliothèques--Histoire.")])),
            ("330", False, (" ", " ", [("a", "Une synthèse claire du sujet.")])),
        ],
    )


def _marcxml_collection(records_xml: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<collection xmlns="http://www.loc.gov/MARC21/slim">
  {records_xml}
</collection>""".encode()


def _marcxml_record(rec_id: str = "MXR-1") -> str:
    return f"""
<record>
  <leader>     nam a2200000   4500</leader>
  <controlfield tag="001">{rec_id}</controlfield>
  <datafield tag="100" ind1="1" ind2=" ">
    <subfield code="a">Lovelace, Ada</subfield>
  </datafield>
  <datafield tag="245" ind1="1" ind2="0">
    <subfield code="a">Notes on the Analytical Engine</subfield>
  </datafield>
  <datafield tag="260" ind1=" " ind2=" ">
    <subfield code="b">Royal Society</subfield>
    <subfield code="c">1843</subfield>
  </datafield>
</record>
"""


# ---------------------------------------------------------------------------
# ISO 2709 low-level parser
# ---------------------------------------------------------------------------


def test_iso2709_round_trips_control_and_data_fields() -> None:
    records = list(iter_iso2709_records(_marc21_book()))
    assert len(records) == 1
    rec = records[0]
    assert rec.get_control("001") == "BK-0001"
    assert rec.get_subfield("245", "a") == "A Tale of Two Libraries :"
    assert rec.get_subfield("245", "c") == "Jane Doe"
    assert rec.get_subfield("100", "a") == "Doe, Jane"
    assert rec.get_subfields("700", "a") == ["Smith, John"]


def test_iso2709_rejects_short_record() -> None:
    from app.errors import AppError

    with pytest.raises(AppError):
        parse_iso2709_record(b"too short")


def test_iso2709_skips_malformed_record_in_stream() -> None:
    # Concatenate a valid + garbage record; stream should yield only valid.
    valid = _marc21_book()
    garbage = b"this is not marc\x1d"
    records = list(iter_iso2709_records(garbage + valid))
    assert len(records) == 1
    assert records[0].get_control("001") == "BK-0001"


# ---------------------------------------------------------------------------
# Tag → doc mapping
# ---------------------------------------------------------------------------


def test_marc21_mapping_captures_title_authors_publisher_date() -> None:
    rec = next(iter_iso2709_records(_marc21_book()))
    doc = marc_record_to_doc(rec, flavor="marc21")
    assert doc is not None
    assert doc["id"] == "BK-0001"
    assert doc["type"] == "bibliographic"
    assert doc["title"].startswith("A Tale of Two Libraries")
    assert doc["creators"] == ["Doe, Jane", "Smith, John"]
    assert doc["publisher"] == "ExPress"
    assert doc["date"] == "2023"
    assert doc["isbn"] == "9781234567890"
    assert doc["subject"] == ["Libraries--History"]
    assert "Includes index." in doc["description"]


def test_unimarc_mapping_uses_200_and_700_tags() -> None:
    rec = next(iter_iso2709_records(_unimarc_book()))
    doc = marc_record_to_doc(rec, flavor="unimarc")
    assert doc is not None
    assert doc["id"] == "UNI-0001"
    assert "Histoire des bibliothèques" in doc["title"]
    assert doc["creators"] == ["Durand, Marie", "Martin, Pierre"]
    assert doc["publisher"] == "ExPresse"
    assert doc["date"] == "2023"
    assert doc["isbn"] == "9782345678901"
    assert doc["subject"] == ["Bibliothèques--Histoire"]
    assert "synthèse claire" in doc["description"]


def test_marc_mapping_returns_none_without_001() -> None:
    rec = MarcRecord(
        leader=" " * 24,
        fields=[MarcField(tag="245", subfields=[("a", "Untitled")])],
    )
    assert marc_record_to_doc(rec, flavor="marc21") is None


# ---------------------------------------------------------------------------
# MARCXML
# ---------------------------------------------------------------------------


def test_marcxml_collection_parses_all_records() -> None:
    payload = _marcxml_collection(_marcxml_record("A") + _marcxml_record("B"))
    records = list(iter_marcxml_records(payload))
    assert [r.get_control("001") for r in records] == ["A", "B"]
    doc = marc_record_to_doc(records[0], flavor="marc21")
    assert doc is not None
    assert doc["title"] == "Notes on the Analytical Engine"
    assert doc["creators"] == ["Lovelace, Ada"]


def test_marcxml_bare_record_root_is_accepted() -> None:
    payload = b"""<?xml version="1.0" encoding="UTF-8"?>
<record xmlns="http://www.loc.gov/MARC21/slim">
  <controlfield tag="001">BARE-1</controlfield>
  <datafield tag="245" ind1="1" ind2="0">
    <subfield code="a">Bare root</subfield>
  </datafield>
</record>"""
    records = list(iter_marcxml_records(payload))
    assert len(records) == 1
    assert records[0].get_control("001") == "BARE-1"


def test_marcxml_rejects_malformed_xml() -> None:
    from app.errors import AppError

    with pytest.raises(AppError):
        list(iter_marcxml_records(b"<not really xml"))


# ---------------------------------------------------------------------------
# Flat-file ingest
# ---------------------------------------------------------------------------


def test_ingest_marc_file_streams_through_bulk_index(tmp_path: Path) -> None:
    path = tmp_path / "records.mrc"
    path.write_bytes(_marc21_book() + _marc21_book())  # two records

    seen: list[list[dict]] = []

    def _bulk(docs: list[dict]) -> tuple[int, int]:
        seen.append(list(docs))
        return len(docs), 0

    result = ingest_marc_file(path=path, bulk_index=_bulk, flavor="marc21", chunk_size=1)
    assert result.ingested == 2
    assert result.failed == 0
    assert result.error is None
    assert [len(c) for c in seen] == [1, 1]


def test_ingest_marcxml_file_streams_records(tmp_path: Path) -> None:
    path = tmp_path / "records.xml"
    path.write_bytes(_marcxml_collection(_marcxml_record("X") + _marcxml_record("Y")))
    seen: list[dict] = []

    def _bulk(docs: list[dict]) -> tuple[int, int]:
        seen.extend(docs)
        return len(docs), 0

    result = ingest_marcxml_file(path=path, bulk_index=_bulk, flavor="marc21")
    assert result.ingested == 2
    assert {d["id"] for d in seen} == {"X", "Y"}


def test_ingest_marc_file_missing_path(tmp_path: Path) -> None:
    result = ingest_marc_file(path=tmp_path / "nope.mrc", bulk_index=lambda _d: (0, 0))
    assert result.ingested == 0
    assert result.error is not None
    assert "MARC file not found" in result.error


def test_ingest_marcxml_file_malformed(tmp_path: Path) -> None:
    path = tmp_path / "broken.xml"
    path.write_text("<not really xml")
    result = ingest_marcxml_file(path=path, bulk_index=lambda _d: (0, 0))
    assert result.ingested == 0
    assert result.error is not None
    assert "not valid XML" in result.error


# ---------------------------------------------------------------------------
# CSV importer
# ---------------------------------------------------------------------------


def test_csv_parse_yields_docs_with_header_columns() -> None:
    payload = (
        b"id,title,description,creators\n"
        b"1,Alpha,First entry,Durand, Marie|Martin, Pierre\n"
        b"2,Beta,Second,Solo Author\n"
    )
    # Note: we use ";" as separator in a different test; this one uses ","
    # but creators column has embedded commas → we switch to the pipe form
    # instead for this test.
    payload = (
        b"id;title;description;creators\n1;Alpha;First entry;Durand|Martin\n2;Beta;Second;Solo\n"
    )
    docs = list(parse_csv_bytes(payload))
    assert [d["id"] for d in docs] == ["1", "2"]
    assert docs[0]["title"] == "Alpha"
    assert docs[0]["creators"] == ["Durand", "Martin"]
    assert docs[1]["creators"] == ["Solo"]
    assert docs[0]["type"] == "record"  # default


def test_csv_parse_rejects_missing_id_column() -> None:
    from app.errors import AppError

    with pytest.raises(AppError):
        list(parse_csv_bytes(b"title,description\nA,B"))


def test_csv_parse_skips_rows_without_id() -> None:
    payload = b"id,title\n,Empty id\n42,Valid\n"
    docs = list(parse_csv_bytes(payload))
    assert [d["id"] for d in docs] == ["42"]


def test_csv_parse_accepts_utf8_bom_and_empty_trailing_cells() -> None:
    payload = "﻿id,title,creators\n1,Only title,\n".encode()
    docs = list(parse_csv_bytes(payload))
    assert len(docs) == 1
    assert docs[0]["title"] == "Only title"
    # Empty creators column should be dropped (no key)
    assert "creators" not in docs[0]


def test_ingest_csv_file_streams(tmp_path: Path) -> None:
    path = tmp_path / "catalogue.csv"
    path.write_text("id,title,type\n1,Alpha,article\n2,Beta,article\n")
    seen: list[dict] = []
    result = ingest_csv_file(
        path=path,
        bulk_index=lambda docs: (seen.extend(docs) or len(docs), 0),
    )
    assert result.ingested == 2
    assert {d["id"] for d in seen} == {"1", "2"}
    # Explicit type column overrides the default.
    assert {d["type"] for d in seen} == {"article"}


def test_ingest_csv_file_missing_path(tmp_path: Path) -> None:
    result = ingest_csv_file(path=tmp_path / "nope.csv", bulk_index=lambda _d: (0, 0))
    assert result.error is not None
    assert "CSV file not found" in result.error


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


class _FakeSource:
    def __init__(self, **kw):
        self.kind = kw.pop("kind", "oaipmh")
        self.url = kw.pop("url", None)
        self.metadata_prefix = kw.pop("metadata_prefix", None)
        self.set_spec = kw.pop("set_spec", None)


def test_dispatcher_runs_marc_file(tmp_path: Path) -> None:
    path = tmp_path / "rec.mrc"
    path.write_bytes(_marc21_book())
    src = _FakeSource(kind="marc_file", url=str(path), metadata_prefix="marc21")
    seen: list[dict] = []

    def _bulk(docs):
        seen.extend(docs)
        return len(docs), 0

    result = run_import(src, bulk_index=_bulk)
    assert result.ingested == 1
    assert seen[0]["id"] == "BK-0001"


def test_dispatcher_runs_unimarc_file_via_flavor(tmp_path: Path) -> None:
    path = tmp_path / "rec.mrc"
    path.write_bytes(_unimarc_book())
    src = _FakeSource(kind="marc_file", url=str(path), metadata_prefix="unimarc")
    seen: list[dict] = []

    def _bulk(docs):
        seen.extend(docs)
        return len(docs), 0

    result = run_import(src, bulk_index=_bulk)
    assert result.ingested == 1
    assert seen[0]["id"] == "UNI-0001"
    assert "Histoire des bibliothèques" in seen[0]["title"]


def test_dispatcher_runs_marcxml_file(tmp_path: Path) -> None:
    path = tmp_path / "rec.xml"
    path.write_bytes(_marcxml_collection(_marcxml_record("MX-1")))
    src = _FakeSource(kind="marcxml_file", url=str(path))
    seen: list[dict] = []

    def _bulk(docs):
        seen.extend(docs)
        return len(docs), 0

    result = run_import(src, bulk_index=_bulk)
    assert result.ingested == 1
    assert seen[0]["id"] == "MX-1"


def test_dispatcher_runs_csv_file(tmp_path: Path) -> None:
    path = tmp_path / "rec.csv"
    path.write_text("id,title\n1,One\n2,Two\n")
    src = _FakeSource(kind="csv_file", url=str(path))
    seen: list[dict] = []

    def _bulk(docs):
        seen.extend(docs)
        return len(docs), 0

    result = run_import(src, bulk_index=_bulk)
    assert result.ingested == 2
    assert {d["id"] for d in seen} == {"1", "2"}


def test_dispatcher_rejects_empty_url_for_new_kinds() -> None:
    for kind in ("marc_file", "marcxml_file", "csv_file", "oaipmh_marcxml"):
        src = _FakeSource(kind=kind, url=None)
        result = run_import(src, bulk_index=lambda _d: (0, 0))
        assert result.error is not None


# ---------------------------------------------------------------------------
# Admin REST surface
# ---------------------------------------------------------------------------


def test_imports_api_accepts_marc_file_kind(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/admin/v1/imports",
        json={
            "label": "Koha dump",
            "kind": "marc_file",
            "url": "/tmp/koha.mrc",
            "metadata_prefix": "unimarc",
            "schema_profile": "library",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "marc_file"
    assert body["metadata_prefix"] == "unimarc"


def test_imports_api_accepts_csv_file_kind(
    client: TestClient, admin_headers: dict[str, str]
) -> None:
    resp = client.post(
        "/admin/v1/imports",
        json={
            "label": "Archives.csv",
            "kind": "csv_file",
            "url": "/tmp/archives.csv",
            "schema_profile": "archive",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text


def test_imports_api_identify_refuses_marc_file(
    client: TestClient, admin_headers: dict[str, str], tmp_path: Path
) -> None:
    path = tmp_path / "x.mrc"
    path.write_bytes(_marc21_book())
    created = client.post(
        "/admin/v1/imports",
        json={"label": "x", "kind": "marc_file", "url": str(path)},
        headers=admin_headers,
    ).json()
    resp = client.post(f"/admin/v1/imports/{created['id']}/identify", headers=admin_headers)
    assert resp.status_code == 400


def test_imports_api_run_marc_file_end_to_end(
    client: TestClient, admin_headers: dict[str, str], tmp_path: Path
) -> None:
    path = tmp_path / "e2e.mrc"
    path.write_bytes(_marc21_book())
    created = client.post(
        "/admin/v1/imports",
        json={
            "label": "e2e",
            "kind": "marc_file",
            "url": str(path),
            "metadata_prefix": "marc21",
        },
        headers=admin_headers,
    ).json()
    resp = client.post(f"/admin/v1/imports/{created['id']}/run", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["records_ingested"] == 1


# ---------------------------------------------------------------------------
# Admin UI
# ---------------------------------------------------------------------------


def test_imports_ui_form_lists_all_seven_kinds(client: TestClient, admin_ui_session: str) -> None:
    resp = client.get("/admin/ui/imports")
    assert resp.status_code == 200
    for kind in (
        "oaipmh",
        "oaipmh_lido",
        "oaipmh_marcxml",
        "lido_file",
        "marc_file",
        "marcxml_file",
        "csv_file",
    ):
        assert f'value="{kind}"' in resp.text
    # MARC flavor selector
    assert 'name="marc_flavor"' in resp.text
    assert 'value="unimarc"' in resp.text


def test_imports_ui_add_marc_file_persists_flavor(
    client: TestClient, admin_ui_session: str, tmp_path: Path
) -> None:
    path = tmp_path / "ui.mrc"
    path.write_bytes(_marc21_book())
    resp = client.post(
        "/admin/ui/imports/add",
        data={
            "csrf_token": admin_ui_session,
            "label": "ui-marc",
            "kind": "marc_file",
            "url": str(path),
            "marc_flavor": "unimarc",
            "schema_profile": "library",
        },
    )
    assert resp.status_code == 200
    from app.dependencies import container

    assert any(
        s.kind == "marc_file" and s.metadata_prefix == "unimarc"
        for s in container.store.list_import_sources()
    )


def test_imports_ui_add_rejects_unknown_marc_flavor(
    client: TestClient, admin_ui_session: str, tmp_path: Path
) -> None:
    resp = client.post(
        "/admin/ui/imports/add",
        data={
            "csrf_token": admin_ui_session,
            "label": "bad",
            "kind": "marc_file",
            "url": str(tmp_path / "x.mrc"),
            "marc_flavor": "klingon",
            "schema_profile": "library",
        },
    )
    assert resp.status_code == 400
    assert "Unknown MARC flavor" in resp.text
