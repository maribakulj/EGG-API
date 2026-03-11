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


class Record(BaseModel):
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
    raw_identifiers: list[str] = Field(default_factory=list)
    raw_fields: dict[str, Any] | None = None
    timestamps: Timestamps = Field(default_factory=Timestamps)


class SearchResponse(BaseModel):
    total: int
    page: int
    page_size: int
    results: list[Record]
    facets: dict[str, dict[str, int]] = Field(default_factory=dict)
