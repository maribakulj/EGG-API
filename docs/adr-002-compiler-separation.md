# ADR 002 — Separation between EGG-API runtime and document compiler

Status: **Accepted** (2026-04, post-maturity pass)
Supersedes: n/a
Relates to: ADR 001 (async I/O strategy), SPECS §4 (future MCP layer)

## Context

Two review rounds converged on the same question: as EGG-API grows,
should the Python runtime absorb deep semantic work — Linked Art
projection, CIDOC CRM alignment, structured patrimonial dates
("vers 1880", "XVIIIe siècle"), cross-institution agent fusion, rich
provenance tracking — or delegate that to an external transformation
engine?

The project owner has a separate, independent project: a **document
compiler written in Clojure** that translates between GLAM exchange
formats (LIDO, MARC, EAD, CSV, OAI-PMH flavors) and produces a
canonical representation with diagnostics. That compiler already
does the intellectual work EGG-API would otherwise grow into.

Both projects could theoretically implement the same mapping logic.
Without a decision, three failure modes are likely:

1. **Two sources of truth.** LIDO/EAD/MARC mapping implemented
   twice (in `app/importers/*.py` and in Clojure), diverging over
   time as one fixes a bug the other doesn't.
2. **Python-side ambition creep.** Each feature request ("I want
   Linked Art", "I want event structures", "I want agent alignment")
   lands as Python code in `app/schemas/`, `app/mappers/`,
   `app/importers/`. The Record model balloons; the mapper stack
   gains handlers for semantics it was not designed for; FastAPI
   becomes an accidental transformation engine.
3. **Forced coupling.** Institutions that only need a plain API
   gateway ("I have a CSV, give me `/v1/search`") are forced to
   install and operate a JVM / Clojure stack they don't need.

## Decision

We split responsibilities along a hard functional boundary and
codify it.

### EGG-API owns publication

- Public API (`/v1/search`, `/v1/records/{id}`, `/v1/facets`,
  `/v1/suggest`, `/v1/collections`, `/v1/schema`).
- Admin UI, admin API, configuration management, secrets redaction.
- Authentication, API keys, rate limiting, CORS, HSTS, CSP.
- Backend adapter (Elasticsearch, OpenSearch, future Solr).
- Bulk indexing, job scheduling, usage auditing, metrics, logs.
- OpenAPI documentation, weak-ETag caching, HTTP content negotiation.
- Lightweight built-in importers — CSV and OAI-PMH Dublin Core — as
  a zero-dependency path for simple deployments.

### The document compiler owns transformation

- Format parsing for rich formats (LIDO, EAD, MARCXML / MARC-binary)
  beyond the "extract flat fields" level EGG's built-in importers
  already cover.
- Canonical model construction (objects, agents, events, places,
  dates, digital resources, rights, identifiers).
- Semantic profile projection: Linked Art JSON-LD, CIDOC CRM RDF,
  schema.org extensions, future profiles.
- Patrimonial date normalisation ("vers 1880" → start/end/qualifier).
- Mapping diagnostics (unmapped fields, lossy conversions,
  provenance of each emitted value).
- Import preview: "what would the canonical output look like" before
  ingestion commits.

### Contract

- The compiler is **optional**. EGG-API boots and serves without it.
- Integration is external: CLI subprocess or HTTP sidecar, configured
  by the admin. No in-process JVM, no Python-side re-implementation
  of the compiler's pipeline.
- The compiler owns the canonical JSON schema (`egg-canonical/<vN>`).
  EGG-API reads and stores compiler outputs but does not define the
  shape.
- Advanced import sources will gain an `engine=compiler` mode
  alongside the existing `engine=builtin` path. Both co-exist.

## What this means for the Python codebase

### Frozen scope (do not add to `app/`)

- **No Linked Art projection in Python.** `app/public_api/jsonld.py`
  stays as the minimal schema.org JSON-LD flavor it is today.
- **No CanonicalAgent / CanonicalEvent / CanonicalPlace / CanonicalDate
  model** in `app/schemas/`. `Record` stays the public publication
  shape, not an intellectual model of the object.
- **No further enrichment of `MuseumFields` / `ArchiveFields`.** The
  current blocks are a pragmatic minimum for library / museum /
  archive profiles on a plain-API deployment; richer representations
  belong to the compiler's output.
- **No patrimonial date normalisation library in Python.** The
  interval-overlap query on `date.start` / `date.end` assumes the
  importer (compiler or caller-side) already normalised dates. EGG
  does not parse "vers 1880" itself.
- **The built-in LIDO / EAD / MARC importers are in maintenance.**
  They stay functional as a fallback for minimal deployments but
  do not grow new field coverage. New format features land in the
  compiler.

### Explicitly deferred until the compiler's CLI / HTTP contract is stable

- `app/compiler/` abstraction layer (`CompilerAdapter`, `CompileResult`,
  FakeCompiler test double).
- `engine="compiler"` import source kind + storage of
  `canonical.json` / `diagnostics.json` artefacts per import run.
- `/admin/v1/imports/{id}/preview` endpoint.
- Import-model refactor from the current flat `ImportSource.kind`
  to the cleaner `(transport, source_format, engine, target_profile)`
  decomposition.

Pre-building these ahead of the compiler's real interface would
produce speculative abstractions that inevitably mismatch the actual
integration. We wait for the compiler side to expose a stable
contract, then do the adapter + refactor work against a real
counterparty.

## Consequences

**Positive.**

- EGG-API stays focused on being a robust GLAM API gateway. The
  existing scope (API, admin, imports for CSV / OAI-DC, indexing,
  caching, security) is finishable without growing.
- The compiler stays focused on what it is good at — semantic
  transformation — without having to grow an HTTP API, admin UI,
  rate limiter or authentication stack.
- Institutions get a gradient: minimal deployments use EGG alone
  with built-in importers; advanced deployments add the compiler
  for richer format support and semantic outputs.
- Source-of-truth ownership is unambiguous: transformation belongs
  to the compiler, publication belongs to EGG-API, and the canonical
  JSON schema is versioned in the compiler's repo.

**Trade-offs we accept.**

- Rich semantic features are gated on the compiler landing. Users
  who want Linked Art today cannot get it from EGG alone; they get
  it when the compiler is ready.
- The two built-in importers for LIDO / EAD / MARC stay intentionally
  shallow. Users with serious LIDO / EAD / MARC needs are routed to
  the compiler when it lands; until then, they get the flat-field
  subset or they pre-convert to CSV / OAI-DC.
- Integration work — `CompilerAdapter`, artefact storage, preview —
  is deferred, not designed now. That's a deliberate choice to avoid
  speculative architecture.

**Revisit if.**

- The compiler project stalls for more than a year, in which case
  the GLAM interop ambition moves back into Python (and we accept
  the weight).
- A third party contributes a Linked Art exporter or a canonical
  model implementation that is independently useful — we would
  reconsider whether "compiler-only" is still the right boundary.
