from __future__ import annotations

from pydantic import BaseModel, Field


class NormalizedQuery(BaseModel):
    q: str | None = None
    page: int = 1
    page_size: int = 20
    sort: str | None = None
    facets: list[str] = Field(default_factory=list)
    include_fields: list[str] = Field(default_factory=list)
    filters: dict[str, list[str]] = Field(default_factory=dict)
    date_from: str | None = None
    date_to: str | None = None
    has_digital: bool | None = None
    has_iiif: bool | None = None

    def depth(self) -> int:
        return self.page * self.page_size
