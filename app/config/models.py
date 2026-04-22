from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Structural public fields that are synthesized by the mapper regardless of
# the declared mapping block; they always count as "mapped".
_STRUCTURAL_FIELDS = {"id", "type"}

# Type aliases: drive OpenAPI enums and narrow mypy's view of the value.
Criticality = Literal["required", "recommended", "optional"]
PublicAuthMode = Literal["anonymous_allowed", "api_key_optional", "api_key_required"]
CorsMode = Literal["off", "allowlist", "wide_open"]
SameSite = Literal["strict", "lax", "none"]
MappingMode = Literal[
    "direct",
    "constant",
    "split_list",
    "first_non_empty",
    "template",
    "nested_object",
    "date_parser",
    "boolean_cast",
    "url_passthrough",
]
BackendType = Literal["elasticsearch", "opensearch"]
BackendAuthMode = Literal["none", "basic", "bearer", "api_key"]


# Reject typos and stale fields everywhere in the config tree.  Silently
# dropping an unknown key (Pydantic's default) turned renames into latent
# bugs: operators kept editing a field the model no longer read.  "forbid"
# fails config-load fast and makes the mistake visible at ``egg-api
# check-config`` time.
_StrictModel: ConfigDict = ConfigDict(extra="forbid")


class SecurityProfile(BaseModel):
    model_config = _StrictModel

    allow_empty_query: bool = False
    page_size_default: int = 20
    page_size_max: int = 50
    max_facets: int = 3
    max_buckets_per_facet: int = 20
    allow_raw_fields: bool = False
    allow_debug_translation: bool = False
    max_depth: int = 2000


class BackendAuthConfig(BaseModel):
    """How the adapter authenticates to the search backend.

    Never store the raw secret in YAML: use an environment variable and
    reference it via ``*_env``. ``ConfigManager.save()`` strips the
    inline ``password``/``token`` fields before writing, so if they
    appear in the in-memory config they never reach disk.
    """

    model_config = _StrictModel

    mode: BackendAuthMode = "none"
    username: str | None = None
    # Inline secrets are accepted (e.g. during a ``PUT /admin/v1/config``
    # round-trip in memory) but always redacted by the manager on save.
    password: str | None = None
    token: str | None = None
    # Preferred indirection: the adapter reads the secret from these env
    # vars at build time.  Values are the *variable name*, not the secret.
    password_env: str | None = Field(default=None, description="Env var name holding the password")
    token_env: str | None = Field(
        default=None, description="Env var name holding the bearer/api_key"
    )

    @model_validator(mode="after")
    def _validate_mode(self) -> BackendAuthConfig:
        if self.mode == "basic":
            if not self.username:
                raise ValueError("backend.auth.mode='basic' requires a username")
            if not (self.password or self.password_env):
                raise ValueError(
                    "backend.auth.mode='basic' requires either password or password_env"
                )
        elif self.mode in {"bearer", "api_key"}:
            if not (self.token or self.token_env):
                raise ValueError(
                    f"backend.auth.mode={self.mode!r} requires either token or token_env"
                )
        return self

    def resolve_password(self) -> str | None:
        if self.password_env:
            return os.getenv(self.password_env)
        return self.password

    def resolve_token(self) -> str | None:
        if self.token_env:
            return os.getenv(self.token_env)
        return self.token


class BackendConfig(BaseModel):
    model_config = _StrictModel

    type: BackendType = "elasticsearch"
    url: str = "http://localhost:9200"
    index: str = "records"
    timeout_seconds: float = 15.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.2
    retry_backoff_cap_seconds: float = 5.0
    retry_deadline_seconds: float = 30.0
    auth: BackendAuthConfig = Field(default_factory=BackendAuthConfig)


class CacheConfig(BaseModel):
    model_config = _StrictModel

    public_max_age_seconds: int = 60
    enabled: bool = True


class RateLimitConfig(BaseModel):
    model_config = _StrictModel

    public_max_requests: int = 60
    public_window_seconds: int = 60
    admin_login_max_requests: int = 10
    admin_login_window_seconds: int = 300


class AuthConfig(BaseModel):
    model_config = _StrictModel

    public_mode: PublicAuthMode = "anonymous_allowed"
    bootstrap_admin_key: str = ""
    admin_cookie_secure: bool = True
    admin_cookie_samesite: SameSite = "strict"
    admin_session_ttl_hours: int = 12
    # Idle timeout: kick admin UI sessions that have not seen any
    # request for ``admin_session_idle_timeout_minutes``. 0 disables
    # the check (legacy behaviour). Sprint 18 default: 15 minutes.
    admin_session_idle_timeout_minutes: int = 15
    # Max sequential 401s a public-API caller may trigger before EGG
    # starts refusing *every* subsequent request from the same IP
    # with 429 for ``public_401_lockout_window_seconds``. Set to 0
    # to disable the lockout entirely.
    public_401_lockout_threshold: int = 20
    public_401_lockout_window_seconds: int = 300


class ProxyConfig(BaseModel):
    model_config = _StrictModel

    # Trust X-Forwarded-* / Forwarded headers only from explicit hop IPs.
    # Leave empty (the default) to disable proxy-header rewriting entirely;
    # set to ["*"] only when the service is guaranteed to be reachable solely
    # through the reverse proxy. Accepts individual IPs or CIDR-like strings.
    trusted_proxies: list[str] = Field(default_factory=list)
    # Host names the app will answer to.  Starlette's TrustedHostMiddleware
    # compares the ``Host`` header against this list (wildcards allowed,
    # e.g. ``*.example.org``).  Empty disables the check — fine for local
    # dev, risky in production behind a shared proxy.
    allowed_hosts: list[str] = Field(default_factory=list)


class CorsConfig(BaseModel):
    model_config = _StrictModel

    mode: CorsMode = "off"
    allow_origins: list[str] = Field(default_factory=list)
    allow_methods: list[str] = Field(default_factory=lambda: ["GET"])
    allow_headers: list[str] = Field(default_factory=lambda: ["x-api-key", "content-type"])


class StorageConfig(BaseModel):
    model_config = _StrictModel

    sqlite_path: str = "data/egg_state.sqlite3"
    # Retention knobs for the background purge task. Set to 0 to disable.
    usage_events_retention_days: int = 30
    purge_interval_seconds: int = 3600


class FieldMapping(BaseModel):
    model_config = _StrictModel

    source: str | None = None
    mode: MappingMode = "direct"
    constant: str | None = None
    sources: list[str] = Field(default_factory=list)
    separator: str = ";"
    template: str | None = None
    # Mode ``template`` only. Explicit allowlist of backend field names
    # the Python ``Template`` may substitute. When empty the mapper
    # falls back to the names literally referenced by the template
    # string itself, which keeps legacy configs working while still
    # refusing to echo any other backend field (Sprint 18 hardening).
    allowed_fields: list[str] = Field(default_factory=list)
    criticality: Criticality = "optional"


class AppConfig(BaseModel):
    model_config = _StrictModel

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
        # Security profile must exist in the profiles dict. Pydantic handles
        # the token-level validation (public_mode, cors.mode, samesite,
        # criticality, mapping mode) via Literal aliases; this validator
        # covers the constraints that span multiple fields.
        if self.security_profile not in self.profiles:
            raise ValueError(
                f"security_profile {self.security_profile!r} is not defined in "
                f"profiles (known: {sorted(self.profiles)})"
            )
        # Browsers ignore `SameSite=None` unless the cookie is also marked
        # Secure. Accepting the combination `none` + secure=false silently
        # leaves the admin session cookie unusable in real browsers — refuse
        # it up-front so the operator finds out at config-load time.
        if self.auth.admin_cookie_samesite == "none" and not self.auth.admin_cookie_secure:
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
