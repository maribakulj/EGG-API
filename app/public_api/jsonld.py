"""Minimal JSON-LD projection for public Records.

Clients that negotiate ``application/ld+json`` or pass ``?format=jsonld``
get a response framed with a small ``@context`` map and ``@type`` drawn
from ``schema.org``. The mapping deliberately covers only the stable
subset of :class:`~app.schemas.record.Record`; GLAM-specific extensions
(IIIF links, holding institution) are exposed verbatim so consumers can
rely on structural stability across deployments.

Kept module-local (not pushed into the Record schema) so the JSON
response of ``/v1/search`` / ``/v1/records/{id}`` stays the canonical
Pydantic shape; JSON-LD is a view on top of it.
"""

from __future__ import annotations

from typing import Any

from app.schemas.record import Record

JSONLD_MEDIA_TYPE = "application/ld+json"

_CONTEXT: dict[str, str] = {
    "@vocab": "https://schema.org/",
    "dcterms": "http://purl.org/dc/terms/",
    "egg": "https://egg-api.example/vocab#",
    "id": "@id",
    "type": "@type",
    "title": "name",
    "description": "description",
    "creators": "creator",
    "contributors": "contributor",
    "languages": "inLanguage",
    "subjects": "about",
    "keywords": "keywords",
    "identifiers": "identifier",
    "links": "egg:links",
    "rights": "license",
    "availability": "egg:availability",
    "collection": "isPartOf",
    "holding_institution": "provider",
    "date": "temporal",
    "timestamps": "egg:timestamps",
    "raw_fields": "egg:rawFields",
    "media": "associatedMedia",
}


def record_to_jsonld(record: Record) -> dict[str, Any]:
    """Return a JSON-LD document for ``record``.

    ``@context`` is inlined rather than referenced by URL so consumers
    never need to resolve a second HTTP hop. The payload is
    ``json``-serializable and stable field-for-field with ``Record``.
    """
    data = record.model_dump(mode="python", exclude_none=False)
    data["@context"] = _CONTEXT
    return data


def search_to_jsonld(
    results: list[Record],
    *,
    total: int,
    page: int,
    page_size: int,
    facets: dict[str, dict[str, int]],
    next_cursor: str | None,
) -> dict[str, Any]:
    """Wrap a result set in a ``SearchResultList`` JSON-LD envelope."""
    return {
        "@context": {**_CONTEXT, "results": "itemListElement", "total": "numberOfItems"},
        "@type": "ItemList",
        "total": total,
        "page": page,
        "page_size": page_size,
        "results": [record_to_jsonld(r) for r in results],
        "facets": facets,
        "next_cursor": next_cursor,
    }
