from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class DateInfo(BaseModel):
    display: str | None = None
    sort: str | None = None
    start: str | None = None
    end: str | None = None


class LabelRef(BaseModel):
    id: str | None = None
    label: str | None = None


class Identifiers(BaseModel):
    source_id: str | None = None
    ark: str | None = None
    doi: str | None = None
    isbn: str | None = None
    issn: str | None = None
    call_number: str | None = None


class Links(BaseModel):
    source: str | None = None
    thumbnail: str | None = None
    preview: str | None = None
    iiif_manifest: str | None = None
    iiif_image: str | None = None
    rights: str | None = None


class RightsInfo(BaseModel):
    label: str | None = None
    uri: str | None = None
    license: str | None = None


class Availability(BaseModel):
    public: bool = False
    digital: bool = False
    iiif: bool = False


class Timestamps(BaseModel):
    indexed_at: str | None = None
    updated_at: str | None = None


class MuseumFields(BaseModel):
    """Museum / archive-oriented fields (Sprint 23).

    None of these are ever required. A library-only deployment never
    maps them, so ``museum`` stays ``None`` on the wire — the public
    schema does not grow for bibliothèques who don't need it.

    An institution mapping any one of them (``inventory_number``,
    ``medium``, …) gets a populated ``museum`` block next to the core
    fields. Frontends can decide per field whether to render.
    """

    inventory_number: str | None = None
    artist: str | None = None  # label-friendly alias of the lead creator
    medium: str | None = None
    dimensions: str | None = None
    acquisition_date: str | None = None
    current_location: str | None = None


class Record(BaseModel):
    """Public record shape.

    The defaults are empty collections / None: only the keys the operator has
    explicitly wired through ``mapping`` (see :mod:`app.mappers.schema_mapper`)
    carry values on the wire. ``raw_identifiers`` was dropped in Sprint 5 —
    ``identifiers`` already covers the same ground more descriptively.

    List/dict fields (``contributors``, ``media``, ``keywords``…) can be
    populated today via ``split_list`` / ``first_non_empty`` / ``nested_object``
    mapping rules; the schema keeps them documented so clients can read a
    stable shape even when a given deployment only fills a subset.
    """

    id: str
    type: str
    title: str | None = None
    subtitle: str | None = None
    description: str | None = None
    creators: list[str] = Field(default_factory=list)
    contributors: list[str] = Field(default_factory=list)
    date: DateInfo = Field(default_factory=DateInfo)
    languages: list[str] = Field(default_factory=list)
    subjects: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    collection: LabelRef = Field(default_factory=LabelRef)
    holding_institution: LabelRef = Field(default_factory=LabelRef)
    identifiers: Identifiers = Field(default_factory=Identifiers)
    links: Links = Field(default_factory=Links)
    media: list[dict[str, Any]] = Field(default_factory=list)
    rights: RightsInfo = Field(default_factory=RightsInfo)
    availability: Availability = Field(default_factory=Availability)
    raw_fields: dict[str, Any] | None = None
    timestamps: Timestamps = Field(default_factory=Timestamps)
    # Sprint 23: museum / archive-oriented fields. Stays ``None`` when
    # none of the inner fields are mapped, so a library-only deployment
    # does not emit an empty ``"museum": {...}`` block.
    museum: MuseumFields | None = None


class SearchResponse(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[Record]
    facets: dict[str, dict[str, int]] = Field(default_factory=dict)
    # Opaque URL-safe token. Present when the backend returned a full page
    # and cursor pagination is being used; ``None`` when callers are paging
    # via ``page=`` or when the result set is exhausted.
    next_cursor: str | None = None
