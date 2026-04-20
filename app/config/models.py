from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

_VALID_CRITICALITIES = {"required", "recommended", "optional"}
_VALID_AUTH_MODES = {"anonymous_allowed", "api_key_optional", "api_key_required"}
_VALID_CORS_MODES = {"off", "allowlist", "wide_open"}
_VALID_SAMESITE = {"strict", "lax", "none"}
# Structural public fields that are synthesized by the mapper regardless of
# the declared mapping block; they always count as "mapped".
_STRUCTURAL_FIELDS = {"id", "type"}


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
    timeout_seconds: float = 15.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.2
    retry_backoff_cap_seconds: float = 5.0
    retry_deadline_seconds: float = 30.0


class CacheConfig(BaseModel):
    public_max_age_seconds: int = 60
    enabled: bool = True


class RateLimitConfig(BaseModel):
    public_max_requests: int = 60
    public_window_seconds: int = 60
    admin_login_max_requests: int = 10
    admin_login_window_seconds: int = 300


class AuthConfig(BaseModel):
    public_mode: str = (
        "anonymous_allowed"  # anonymous_allowed | api_key_optional | api_key_required
    )
    bootstrap_admin_key: str = ""
    admin_cookie_secure: bool = True
    admin_cookie_samesite: str = "strict"
    admin_session_ttl_hours: int = 12


class ProxyConfig(BaseModel):
    # Trust X-Forwarded-* / Forwarded headers only from explicit hop IPs.
    # Leave empty (the default) to disable proxy-header rewriting entirely;
    # set to ["*"] only when the service is guaranteed to be reachable solely
    # through the reverse proxy. Accepts individual IPs or CIDR-like strings.
    trusted_proxies: list[str] = Field(default_factory=list)


class CorsConfig(BaseModel):
    mode: str = "off"  # off | allowlist | wide_open
    allow_origins: list[str] = Field(default_factory=list)
    allow_methods: list[str] = Field(default_factory=lambda: ["GET"])
    allow_headers: list[str] = Field(default_factory=lambda: ["x-api-key", "content-type"])


class StorageConfig(BaseModel):
    sqlite_path: str = "data/egg_state.sqlite3"


class FieldMapping(BaseModel):
    source: str | None = None
    mode: str = "direct"
    constant: str | None = None
    sources: list[str] = Field(default_factory=list)
    separator: str = ";"
    template: str | None = None
    criticality: str = "optional"

    @model_validator(mode="after")
    def _validate_criticality(self) -> FieldMapping:
        if self.criticality not in _VALID_CRITICALITIES:
            raise ValueError(
                f"criticality must be one of {sorted(_VALID_CRITICALITIES)}; "
                f"got {self.criticality!r}"
            )
        return self


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
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    cors: CorsConfig = Field(default_factory=CorsConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    allowed_sorts: list[str] = Field(
        default_factory=lambda: ["relevance", "date_desc", "date_asc", "title_asc"]
    )
    allowed_facets: list[str] = Field(
        default_factory=lambda: ["type", "language", "collection", "institution", "subject"]
    )
    allowed_include_fields: list[str] = Field(
        default_factory=lambda: ["id", "type", "title", "description", "creators"]
    )
    mapping: dict[str, FieldMapping] = Field(
        default_factory=lambda: {
            "id": FieldMapping(source="id", mode="direct", criticality="required"),
            "type": FieldMapping(source="type", mode="direct", criticality="required"),
            "title": FieldMapping(source="title", mode="direct"),
            "description": FieldMapping(source="description", mode="direct"),
            "creators": FieldMapping(source="creator_csv", mode="split_list", separator=";"),
        }
    )

    @model_validator(mode="after")
    def _validate_cross_references(self) -> AppConfig:
        # Security profile must exist in the profiles dict.
        if self.security_profile not in self.profiles:
            raise ValueError(
                f"security_profile {self.security_profile!r} is not defined in "
                f"profiles (known: {sorted(self.profiles)})"
            )
        # Auth public_mode must be one of the declared values.
        if self.auth.public_mode not in _VALID_AUTH_MODES:
            raise ValueError(
                f"auth.public_mode must be one of {sorted(_VALID_AUTH_MODES)}; "
                f"got {self.auth.public_mode!r}"
            )
        # CORS mode must be a known token.
        if self.cors.mode not in _VALID_CORS_MODES:
            raise ValueError(
                f"cors.mode must be one of {sorted(_VALID_CORS_MODES)}; got {self.cors.mode!r}"
            )
        # Cookie SameSite must be a known token.
        samesite = (self.auth.admin_cookie_samesite or "").lower()
        if samesite not in _VALID_SAMESITE:
            raise ValueError(
                f"auth.admin_cookie_samesite must be one of {sorted(_VALID_SAMESITE)}; "
                f"got {self.auth.admin_cookie_samesite!r}"
            )
        # Browsers ignore `SameSite=None` unless the cookie is also marked
        # Secure. Accepting the combination `none` + secure=false silently
        # leaves the admin session cookie unusable in real browsers — refuse
        # it up-front so the operator finds out at config-load time.
        if samesite == "none" and not self.auth.admin_cookie_secure:
            raise ValueError(
                "auth.admin_cookie_samesite='none' requires admin_cookie_secure=true; "
                "set both or pick 'lax'/'strict'"
            )
        # Every allowed_include_field must either be a structural field or be
        # explicitly declared in the mapping block — otherwise the API surface
        # references a field the mapper will always return None for.
        mapped_fields = _STRUCTURAL_FIELDS | set(self.mapping.keys())
        unmapped = [f for f in self.allowed_include_fields if f not in mapped_fields]
        if unmapped:
            raise ValueError(
                f"allowed_include_fields references fields absent from mapping: {unmapped}"
            )
        # Required/recommended mapping rules must declare a source (or sources
        # for first_non_empty, or a constant/template) — otherwise they can
        # never succeed.
        for name, rule in self.mapping.items():
            if rule.criticality in {"required", "recommended"}:
                has_source = bool(rule.source)
                has_sources = bool(rule.sources)
                has_constant = rule.constant is not None
                has_template = rule.template is not None
                if not (has_source or has_sources or has_constant or has_template):
                    raise ValueError(
                        f"mapping[{name!r}] is {rule.criticality} but declares "
                        "no source/sources/constant/template"
                    )
        return self
