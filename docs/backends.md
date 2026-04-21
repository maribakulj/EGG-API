# Adding a search backend

EGG-API treats the backend as a pluggable read-only index. Every consumer
route talks to the search store through :class:`app.adapters.base.BackendAdapter`,
a runtime-checkable :class:`typing.Protocol`. Adding support for a new
backend (Solr, Meilisearch, Typesense, a custom REST API) means
implementing that Protocol and registering a branch in the factory.

## 1) Understand the contract

Read `app/adapters/base.py`. The Protocol is intentionally minimal:

- `detect()`, `health()`, `list_sources()`, `scan_fields()` — bootstrap /
  admin diagnostics.
- `translate_query(nq, **kwargs)` — turn a `NormalizedQuery` into the
  wire payload. Must not touch the backend.
- `search(nq)`, `get_record(id)`, `get_facets(nq)` — one round-trip each.
  Transient HTTP failures should flow through `AppError("backend_unavailable", 503)`.
- `extract_facets(payload)` — staticmethod, parses the backend's response
  aggregations into the uniform `{facet: {bucket: count}}` shape.

Errors: raise `app.errors.AppError` with a typed code. Use
`egg_backend_errors_total{error_code=...}` conventions from
`app/metrics/__init__.py` — the alerts in `ops/prometheus/alerts.yml`
already know the pre-existing codes.

## 2) Implement the adapter

Create a module under `app/adapters/<name>/adapter.py`. Two patterns:

**Fork-and-extend** (same REST surface with tweaks). The `OpenSearchAdapter`
subclasses `ElasticsearchAdapter` and only overrides `detect()` for the
version floor / distribution label. Total diff: ~40 lines.

**From scratch** (different REST/GRPC surface). Build the retry loop, the
`httpx.Client` with `follow_redirects=False`, and the `X-Opaque-Id`
propagation yourself. Take `retry_backoff_*` + `retry_deadline_seconds`
parameters so the operator can tune them via `backend.*` config.

Either way, the adapter instance must expose a `client` attribute (the
`httpx.Client`) so `Container.reload()` can close the old client when
configuration changes — see `app/dependencies.py`.

## 3) Register it in the factory

`app/adapters/factory.py` dispatches on `backend.type`. Add a branch:

```python
if backend.type == "mynew":
    return MyNewAdapter(backend.url, backend.index, **kwargs)
```

And extend the `BackendType` Literal in `app/config/models.py`:

```python
BackendType = Literal["elasticsearch", "opensearch", "mynew"]
```

The Pydantic validator will now accept `type: mynew` in operator configs
and reject anything else with a clear 400.

## 4) Ship tests

Every new backend must ship:

- **Protocol conformance**:

  ```python
  from app.adapters.base import BackendAdapter
  adapter = MyNewAdapter("http://x", "idx")
  assert isinstance(adapter, BackendAdapter)
  ```

- **Unit tests** behind `httpx.MockTransport`: at minimum `detect()`,
  `search()`, `get_record()`, `get_facets()`, and one failure case per
  error code the adapter is expected to produce. Follow the pattern in
  `tests/security/test_vague2_robustness.py` (H2 tests).

- **Factory wiring**: a test that `build_adapter` returns your class
  when `backend.type == "mynew"`.

## 5) Documentation

Update `README.md` to mention the new backend in the supported list.
If the operator experience differs (e.g. different auth flow, optional
headers) add a note to `INSTALL.md` and — if it affects observability —
to `ops/RUNBOOK.md`.

## Out of scope for a new backend

- The HTTP layer (FastAPI routes, CORS, rate limiter, middleware) is
  backend-agnostic. Don't touch it.
- The schema mapper (`app/mappers/schema_mapper.py`) already handles
  any document shape via the `MappingMode` dispatch dict. Add a new
  mapping mode there, not in the adapter.
- Admin UI, CSV export, OpenAPI contract: stable, no per-backend changes.

If you find yourself wanting to extend any of the above for a specific
backend, it's a sign the Protocol is leaking — talk to the maintainers
before landing the change.
