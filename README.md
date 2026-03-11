# PISCO-API (MVP)

PISCO-API is a plug-and-play API layer for GLAM collections. It sits in front of an existing search backend and exposes a safe, normalized, backend-independent public API contract.

## Purpose

This MVP delivers a production-oriented baseline for institutions that already have searchable data (currently Elasticsearch only) but need a stable public API and setup/admin controls.

## Architecture Overview

- **FastAPI app** (`app/main.py`)
- **Public API** (`app/public_api/routes.py`)
- **Admin API** (`app/admin_api/routes.py`)
- **Query policy layer** (`app/query_policy/engine.py`) for validation and safety enforcement
- **Backend adapter** (`app/adapters/elasticsearch/adapter.py`) for read-only Elasticsearch access
- **Schema mapping** (`app/mappers/schema_mapper.py`) to normalize backend documents to public records
- **Config manager** (`app/config/manager.py`) for YAML config persistence and validation
- **Security**:
  - API keys (`app/auth/api_keys.py`)
  - admin key requirement
  - configurable public auth mode
  - in-memory rate limiting (`app/rate_limit/limiter.py`)

## Current Scope (v1 MVP)

Included:
- Elasticsearch adapter
- Read-only public endpoints
- Admin setup/config endpoints
- YAML config management
- In-memory API key store
- In-memory rate limiting
- Tests (unit + integration + security + contract)

Not included in v1:
- OpenSearch adapter
- Solr adapter
- Web admin UI
- persistent API key store
- Redis-backed rate limiting/cache
- cursor pagination
- OAI-PMH module
- IIIF proxy enhancements


## Runtime requirements

- Python 3.10+
- Elasticsearch backend (v8+ recommended for this MVP)

## Quickstart

```bash
make install
make test
make run
```

Server starts at `http://127.0.0.1:8000`.

## Configuration

Default example: `examples/config.yaml`.

```yaml
backend:
  type: elasticsearch
  url: http://localhost:9200
  index: records
security_profile: prudent
auth:
  public_mode: anonymous_allowed
```

Profiles:
- `prudent`: strict defaults and safer limits
- `standard`: larger page/facet limits

## Public API

Base: `/v1`

- `GET /v1/search`
- `GET /v1/records/{id}`
- `GET /v1/facets`
- `GET /v1/health`
- `GET /v1/openapi.json`

Public query parameters are normalized through the `QueryPolicyEngine`; raw backend DSL is never exposed.

## Admin API

Base: `/admin/v1` (requires `x-api-key`)

- `POST /setup/detect`
- `POST /setup/scan-fields`
- `POST /setup/create-config`
- `GET /config`
- `PUT /config`
- `POST /config/validate`
- `POST /test-query`
- `GET /status`

## Security Model

- Read-only backend adapter behavior
- Admin endpoints require valid API key
- Public auth mode is configurable:
  - `anonymous_allowed`
  - `api_key_optional`
  - `api_key_required`
- In-memory rate limiter applies quota checks
- Unknown params, unsupported sorts/facets, and unsafe deep pagination are explicitly rejected

## Limitations of v1

- Single backend adapter (Elasticsearch)
- Single-instance, single-project assumptions
- page/page_size pagination only
- in-memory auth/rate-limit state (non-persistent)
- no deep pagination workaround by design

## Roadmap

- OpenSearch adapter
- Solr adapter
- Web admin UI
- Persistent API key store
- Redis-backed rate limiting
- Redis-backed caching
- Cursor pagination
- OAI-PMH module
- IIIF proxy hardening
