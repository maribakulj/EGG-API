# Post-audit completion matrix

Every finding from the original audit, tracked against the sprint that
addressed it. Numbering follows the audit sections (§1 critiques, §2
élevé, §3 moyen, §4 contrat, §5 tests, §6 devex, §8 archi).

## Critique (§1)

| # | Finding | Status | Sprint |
|---|---------|--------|--------|
| 1.1 | Rate-limit bucket leaked the raw API key | ✅ fixed | S1.1 |
| 1.2 | Prometheus `endpoint` label cardinality bomb | ✅ fixed | S1.2 |
| 1.3 | No CSRF on admin UI POSTs | ✅ fixed | S2.1 |
| 1.4 | UI session tokens stored in clear | ✅ fixed | S1.6 |
| 1.5 | `SchemaMapper.map_record` → Pydantic 500 on missing id | ✅ fixed | S1.3 |
| 1.6 | Audit middleware missed 500s | ✅ fixed | S1.4 |
| 1.7 | SQLite I/O blocked the event loop | ✅ fixed | S3.3/S3.4 |
| 1.8 | httpx client leaked on `Container.reload()` | ✅ fixed | S1.5 |
| 1.9 | `request.client.host` wrong behind reverse proxy | ✅ fixed | S2.4 |

## Élevé (§2)

| # | Finding | Status | Sprint |
|---|---------|--------|--------|
| 2.1 | `set_key_status(secret OR key_id)` ambiguous | ✅ fixed | S2.7 |
| 2.2 | `/v1/health` exposed cluster internals publicly | ✅ fixed | S1.9 |
| 2.3 | `/metrics` unauthenticated | ✅ fixed | S1.11 |
| 2.4 | `/docs` + `/openapi.json` exposed in prod | ✅ fixed | S1.10 |
| 2.5 | No size cap on `q` / `filters` / `include_fields` | ✅ fixed | S1.8 |
| 2.6 | `x-request-id` accepted verbatim | ✅ fixed | S1.7 |
| 2.7 | Plain SHA-256 on API key hashes | ✅ fixed (opt-in) | S4.6/S4.7 |
| 2.8 | No auto-purge for sessions or `usage_events` | ✅ fixed | S4.3/S4.4 |
| 2.9 | Pydantic traces leaked into admin UI errors | ✅ fixed | S2.3 |
| 2.10 | Naive secrets redaction (single path) | 🟡 partial | tracked |

## Moyen (§3)

| # | Finding | Status | Sprint |
|---|---------|--------|--------|
| 3.1 | `security_headers_middleware` applied to `/admin-static` | ℹ️ wontfix (cosmetic) | — |
| 3.2 | `Vary: x-api-key` on shared cache | ✅ fixed | S5.3 |
| 3.3 | Per-request `sqlite3.connect()` | ✅ fixed | S3.6 |
| 3.4 | Retry backoff unbounded | ✅ fixed | S3.5 |
| 3.5 | 302 from backend surfaced as 503 | ℹ️ kept 503 (correct per adapter spec) | — |
| 3.6 | `mode="python"` on `yaml.safe_dump` | 🟡 deferred | low risk |
| 3.7 | Ad-hoc schema migration | ✅ fixed | S4.1/S4.2 |
| 3.8 | `Container.reload()` partial atomicity | 🟡 accepted | mitigated via `_reload_lock` + client close |
| 3.9 | `_apply_mode` if/elif dispatch | ✅ fixed | S5.2 |
| 3.10 | `_VALID_*` sets instead of Literal/Enum | ✅ fixed | S5.1 |
| 3.11 | `client_host` may be None | ✅ fixed (via S2.4 trusted-proxies) | S2.4 |
| 3.12 | `TestClient` hosts collide under xdist | ✅ fixed (xdist supported) | S7.6 |

## API / Contrat (§4)

| # | Finding | Status | Sprint |
|---|---------|--------|--------|
| 4.1 | `configuration_error` → `bad_gateway` on malformed record | ✅ fixed | S1.3 (S5.4 confirmation) |
| 4.2 | `/v1/suggest` + `/v1/manifest/{id}` = 501 stubs | ✅ fixed | S5.5 (retired) + S8.3 (re-implemented) |
| 4.3 | No cursor pagination | ✅ fixed | S8.1 |
| 4.4 | `Record` fields with no mapper path (`contributors`, `media`, `raw_identifiers`) | ✅ fixed | S5.6 |
| 4.5 | `Record.id: str` required with no fallback | ✅ fixed | S1.3 |
| 4.6 | No content-negotiation (only JSON) | ✅ fixed | S5.8 (CSV) + S8.4 (JSON-LD) |
| 4.7 | No `correlation_id` propagated to backend | ✅ fixed | S5.7 |

## Tests (§5)

| # | Finding | Status | Sprint |
|---|---------|--------|--------|
| 5.1 | Tests share the same `container` singleton | ✅ fixed | S7.2 + S7.6 |
| 5.2 | No load test | ✅ fixed | S6.9 |
| 5.3 | No event-loop blocking test | ✅ fixed | S3.8 |
| 5.4 | `test_c3_invalid_key_falls_back_to_client_host` incomplete | ✅ fixed | S1.1 tests |
| 5.5 | CORS tests only cover `off` | ✅ fixed | S5.10 |
| 5.6 | No integration with real ES | 🟡 deferred | compose stack exists for manual runs |
| 5.7 | `test_rate_limiting_behavior` mutates globals | 🟡 acceptable | isolated via autouse fixture |
| 5.8 | No coverage floor | ✅ fixed | S0 (80% gate) |

## DevEx / Ops (§6)

| # | Finding | Status | Sprint |
|---|---------|--------|--------|
| 6.1 | No Dockerfile / docker-compose | ✅ fixed | S0 |
| 6.2 | No CI | ✅ fixed | S0 (lint + tests + release on tag) |
| 6.3 | No ruff / mypy / black | ✅ fixed | S0 (ruff + mypy) |
| 6.4 | No lock file | ✅ fixed | S0 (pip-compile) |
| 6.5 | AGENTS.md ≠ pyproject Python version | ✅ fixed | S0 |
| 6.6 | `setup.sh --no-build-isolation` surprising | ℹ️ kept (constrained envs) | — |
| 6.7 | No `/livez` vs `/readyz` separation | ✅ fixed | S1.9 |
| 6.8 | No systemd / K8s manifest | ✅ fixed | S6.8 |

## Archi (§8)

| # | Finding | Status | Sprint |
|---|---------|--------|--------|
| 8.1 | Singleton `container` couples everything | 🟡 additive fix | S7.2 (`request.app.state.container` + `get_container`) |
| 8.2 | No `StorageBackend` abstraction | ✅ fixed | S7.3 (4 Protocols) |
| 8.3 | No `BackendAdapter` Protocol | ✅ fixed | S7.1 |
| 8.4 | Not async end-to-end | 🟡 deferred (ADR-001 rationale) | Option B open |
| 8.5 | Config reload rebuilds everything | 🟡 accepted | mitigated by `_reload_lock` + client close |
| 8.6 | No tracing | ✅ fixed (opt-in) | S6.1 |
| 8.7 | Multi-backend gap | ✅ fixed | S7.4 (OpenSearch) + factory |
| 8.8 | Unused `quota_counters` table | ✅ fixed | S4.9 (dropped) |

## Open / deferred

The 🟡 / ℹ️ items above are either explicit trade-offs or are scheduled
for a future minor release:

- **3.6** (`mode="python"` on YAML dump): fragile only if a `Path` /
  `datetime` ever creeps into config; dodged by the current schema.
- **3.8** / **8.5** (`Container.reload()` not fully atomic): serialized
  under a lock and the old `httpx.Client` is explicitly closed; a full
  shared-nothing handler shape is tracked for post-1.0.
- **5.6** (real ES integration): `docker compose up` gives an ES 8
  sandbox, but no automated end-to-end test exists yet. Acceptable for
  MVP; an ES-backed integration tier is a v1.x item.
- **2.10** (secrets redaction single path): add `pydantic.SecretStr`
  across sensitive config fields when new secret values are added.
- **8.4** (fully async): ADR-001 documents why Option A (threadpool)
  was picked. Revisit when the single-node RPS ceiling becomes a
  concern.
