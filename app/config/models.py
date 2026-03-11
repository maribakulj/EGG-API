from __future__ import annotations

from pydantic import BaseModel, Field


class SecurityProfile(BaseModel):
    allow_empty_query: bool = False
    page_size_default: int = 20
    page_size_max: int = 50
    max_facets: int = 3
    max_buckets_per_facet: int = 20
    allow_raw_fields: bool = False
    allow_debug_translation: bool = False
    max_depth: int = 2000


class BackendConfig(BaseModel):
    type: str = "elasticsearch"
    url: str = "http://localhost:9200"
    index: str = "records"


class AuthConfig(BaseModel):
    public_mode: str = "anonymous_allowed"  # anonymous_allowed | api_key_optional | api_key_required
    bootstrap_admin_key: str = "dev-admin-key-change-me"


class StorageConfig(BaseModel):
    sqlite_path: str = "data/pisco_state.sqlite3"


class FieldMapping(BaseModel):
    source: str | None = None
    mode: str = "direct"
    constant: str | None = None
    sources: list[str] = Field(default_factory=list)
    separator: str = ";"
    template: str | None = None
    criticality: str = "optional"


class AppConfig(BaseModel):
    backend: BackendConfig = Field(default_factory=BackendConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    security_profile: str = "prudent"
    profiles: dict[str, SecurityProfile] = Field(
        default_factory=lambda: {
            "prudent": SecurityProfile(),
            "standard": SecurityProfile(page_size_max=100, max_facets=5, max_buckets_per_facet=50),
        }
    )
    auth: AuthConfig = Field(default_factory=AuthConfig)
    allowed_sorts: list[str] = Field(default_factory=lambda: ["relevance", "date_desc", "date_asc", "title_asc"])
    allowed_facets: list[str] = Field(default_factory=lambda: ["type", "language", "collection", "institution", "subject"])
    allowed_include_fields: list[str] = Field(default_factory=lambda: ["id", "type", "title", "description", "creators"])
    mapping: dict[str, FieldMapping] = Field(default_factory=dict)
