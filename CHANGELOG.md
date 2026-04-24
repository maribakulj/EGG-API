# Changelog

All notable changes to EGG-API are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- **Maturity pass — closed four gaps surfaced by an external review.**
  - ``GET /admin/v1/config`` no longer leaks ``auth.bootstrap_admin_key``,
    ``backend.auth.password`` and ``backend.auth.token`` in the clear. The
    redaction policy previously applied only to the YAML save path is now
    shared with the live-introspection path via
    ``ConfigManager.redact(data, mask=True|False)``: ``mask=True``
    replaces non-empty secrets with ``"***"`` (admins still see whether
    a secret is configured), ``mask=False`` removes them entirely (used
    for on-disk persistence and ``/admin/v1/export-config``).
  - The public ``/v1/openapi.json`` now strips ``/admin/*`` paths and
    the ``admin`` tag so anonymous callers cannot fingerprint the
    operator surface. Operators who need the full schema hit the new
    ``/admin/v1/openapi.json`` with their admin key. This is a
    **breaking change** on the public OpenAPI contract — the
    ``tests/snapshots/openapi_paths.json`` drifts from 67 entries to
    14 (public-only).

### Fixed

- **Cursor pagination bootstraps from the first page.** The
  Elasticsearch adapter now always emits a stable ``sort`` clause
  (primary ordering + ``_id`` tie-breaker), so ``hit.sort`` is
  populated on the very first ``/v1/search`` response and the route
  can return a usable ``next_cursor`` to the caller without requiring
  an initial cursor — which was the chicken-and-egg bug that made
  cursor pagination unreachable on a real Elasticsearch backend.
- **``sort``, ``date_from``, ``date_to``, ``include_fields`` are now
  actually applied.** The policy layer validated them but the ES
  adapter ignored all four, so the API was accepting parameters it
  pretended to honor:
  - ``sort`` translates the symbolic ``<field>_asc``/``<field>_desc``
    convention from ``config.allowed_sorts`` (and the reserved
    ``relevance`` alias) into the ES sort clause, always followed by
    ``{"_id": "asc"}`` as a stable tie-breaker.
  - ``date_from`` / ``date_to`` become a ``range`` filter on the
    canonical ``date`` field.
  - ``include_fields`` is applied post-mapping as a sparse-fieldset
    filter on the JSON response (keeping structural ``id`` + ``type``).
    CSV and JSON-LD keep their full shapes — the first has fixed
    columns by design, the second needs the complete ``@context`` map.
- **ETags are now marked weak (``W/"…"``) per RFC 7232** to match
  their actual computation (query-key hash, not body hash).
  ``If-None-Match`` comparison strips the ``W/`` prefix so both flavors
  match, in line with the weak-comparison rule in §2.3.2.
- **Docker build no longer fails on an excluded README.** The
  ``.dockerignore`` ``*.md`` glob now carries a ``!README.md``
  exception — setuptools needs the file at build time because
  ``pyproject.toml`` declares it as the project ``readme``.
- **Wheel installs now ship every template and static asset.** The
  ``package-data`` glob widens from ``admin_ui/templates/*.html`` to
  ``admin_ui/templates/**/*.html`` + ``admin_ui/static/**/*`` +
  ``landing/templates/**/*.html`` + ``landing/static/**/*``. Nested
  setup-wizard templates and the landing page bundle now travel with
  the wheel and the Docker image.
- **Dockerfile pins Python 3.12-slim** (matching the CI matrix
  ceiling) instead of ``python:3.14-slim`` which did not track any CI
  target.

### Added

- **Sprint 30 — Deployment-wide language picker + polish** closes the
  i18n story opened in Sprint 29 and trims the last rough edges before
  release.
  - New ``AppConfig.default_language`` field
    (``Literal["en", "fr"] | None``) lets the admin choose a
    deployment-wide default for the console + landing page. Visitor
    preferences (query, cookie, ``Accept-Language``) keep precedence
    over it; it only kicks in when the visitor expresses no
    preference of their own.
  - Wizard landing page (``/admin/ui/setup``) now renders a two-button
    language picker (English / Français) right at the top. Submitting
    to ``/admin/ui/setup/language`` writes ``config.default_language``
    *and* sets the operator's ``egg_lang`` cookie so they carry the
    pick through the rest of the wizard without re-selecting.
  - ``/admin/ui/config`` gets a "Default interface language" dropdown
    (Auto / English / Français). Empty selection clears the preference
    and lets the resolver fall through to env / browser defaults.
  - Admin shell (``base.html``) is now fully localised: title, sign-in
    line, every nav link, both sign-out buttons, plus an
    "English · Français" switcher in the nav bar. The imports page
    heading, help copy, form heading + label, and the schedule dropdown
    options all flip between English and French.
  - The i18n resolver priority chain grows a fifth step:
    query → cookie → ``Accept-Language`` → **``config.default_language``**
    → ``EGG_DEFAULT_LANG`` → English.
  - Coverage gate tightened back from 77 % to **78 %** now that the
    importer + i18n surface has stabilised.
  - README gains a dedicated "But I don't have anything yet" note
    pointing operators without a SIGB / DAMS at
    [Omeka S](https://omeka.org/s/) — a free open-source CMS that
    exposes OAI-PMH out of the box and plugs into the Sprint 22
    ``oaipmh`` importer with zero code.
  - 17 new tests in ``tests/security/test_sprint30_i18n_config.py``
    cover ``AppConfig.default_language`` validation (en / fr /
    invalid), resolver priority at every step (config default wins
    over env, cookie wins over config default, header wins over
    config default, ``None`` defers to env), wizard language picker
    rendering + POST persistence + cookie write + CSRF guard + 400
    on unsupported value, admin config dropdown rendering + save
    round-trip + empty-value clears the field, admin shell nav
    renders in French via config default, imports page renders in
    French, and the language-switch links round-trip EN ↔ FR in the
    nav bar.

- **Sprint 29 — French i18n + user-journey tests** makes the product
  approachable for the francophone half of the target market (Koha,
  PMB, AtoM, Mnesys, Ligeo deployments across France, Belgium, Quebec,
  francophone Africa) and locks down the full non-technical operator
  flow with end-to-end tests.
  - New ``app/i18n.py`` — stdlib-only translation registry with two
    catalogues (``en`` default, ``fr``) held as plain dicts so
    operators can read and extend them without learning gettext.
    Resolver priority is ``?lang=`` query param → ``egg_lang`` cookie
    → ``Accept-Language`` header → ``EGG_DEFAULT_LANG`` env → English
    fallback; unknown keys degrade to the English text or the raw
    key (never to a crash).
  - Landing page (``/``) and about page (``/about``) now render fully
    in French when the resolver picks it, including the hero, CTAs,
    status tile, cards, the "who is it for", "profiles", "nine
    importers", "what it is not" and "next steps" sections. The
    header nav grows a language switcher ("English · Français") and
    selecting a language via ``?lang=`` writes an ``egg_lang`` cookie
    so the preference follows the operator across pages.
  - ``EGG_DEFAULT_LANG=fr`` lets deployments boot the landing page
    in French out of the box.
  - 19 new tests split across two modules:
    - ``tests/security/test_sprint29_i18n.py`` (13 cases) — resolver
      priority (query > cookie > header > env > default), landing and
      about rendered in French with the ``<html lang="fr">`` marker,
      cookie persistence on ``?lang=``, the language switcher
      appearing in both labels, unsupported ``?lang=de`` falling back
      cleanly without cookie pollution, and the translator's key
      fallback / unknown-language paths.
    - ``tests/security/test_sprint29_user_journey.py`` (6 cases +
      1 scheduler round-trip) — a single non-technical operator's
      walk through the product: anonymous landing, OAI endpoint
      probe, admin console, imports dashboard, CSV flat-file source
      created through the admin UI, source run, ``bulk_index``
      assertion, public ``/v1/search`` still answers. A second case
      replays the flow in French. A third case drives the S27
      scheduler: creates a ``daily`` source, backdates
      ``next_run_at``, runs ``Scheduler.run_pending()``, asserts the
      next run was rolled forward.

- **Sprint 28 — Public landing page + positioning** gives
  non-technical visitors a first page that explains what EGG-API is,
  who it is for, and how to get started — without assuming they
  speak FastAPI, OpenSearch or OAI-PMH.
  - New ``app/landing/`` module with:
    - ``GET /`` — landing page listing the three collection profiles
      (library / museum / archive), the nine importers, and the
      outbound OAI provider; carries a live **Status** tile that
      degrades gracefully when the backend is unreachable.
    - ``GET /about`` — design principles, product positioning,
      "what EGG-API is *not*", and links back to the wizard /
      admin console / OAI / OpenAPI.
    - ``/landing-static/landing.css`` — tiny dedicated stylesheet
      (no CDN, no JS) with a warm archivist-friendly palette.
  - Both routes use ``include_in_schema=False`` so the Sprint 5
    public path snapshot stays stable; ``tests/snapshots/openapi_paths.json``
    is unchanged.
  - 7 new tests in ``tests/security/test_sprint28_landing.py`` cover
    HTML content markers (profiles, importers, CTA copy), outbound
    link targets (admin console, setup wizard, OAI, OpenAPI, search),
    the status-tile happy path + backend-down degradation, the CSS
    asset being served under ``/landing-static``, and the OpenAPI
    exclusion.

- **Sprint 27 — Scheduler + outbound OAI-PMH provider** turns EGG-API
  into a fully autonomous GLAM publication node: catalogues refresh
  themselves on a cadence, and aggregators (Europeana, Gallica,
  Isidore, BASE, OpenAIRE, CollEx) can harvest EGG's indexed content
  over the same OAI-PMH protocol it already consumes.
  - **Import scheduler** (`app/scheduler.py`): a single background
    daemon thread polls ``import_sources`` every tick (default 60 s)
    for rows with ``next_run_at <= now`` and runs them through the
    Sprint 24 ``run_import`` dispatcher. Cadence vocabulary is
    deliberately small (``hourly``, ``6h``, ``daily``, ``weekly``)
    so the admin form stays a dropdown instead of a cron expression
    field. Manual sources (no cadence) are never picked — the
    *Run now* button keeps its full role for ad-hoc testing.
  - Migration 10 adds ``schedule`` + ``next_run_at`` columns on
    ``import_sources`` + a filtered index to keep the due-lookup
    constant-time. ``SQLiteStore`` gains
    ``list_due_import_sources`` and ``set_import_source_schedule``.
  - FastAPI lifespan starts / stops the scheduler automatically.
    ``EGG_SCHEDULER=off`` disables it per deployment (useful for
    read-only replicas) and ``EGG_SCHEDULER_TICK_SECONDS`` tunes
    the poll cadence for local development.
  - Admin REST payload: ``CreateImportSourceRequest.schedule``
    accepts the four allowed cadence values or ``null``.
    ``ImportSourceResponse`` now echoes ``schedule`` + ``next_run_at``
    so the UI dashboard can display the next firing time.
  - Admin UI imports form grows a **Run schedule** dropdown and the
    source table shows ``Schedule`` + ``Next run`` columns alongside
    the existing ``Last run``.
  - **Outbound OAI-PMH provider** (`app/oai_provider.py` +
    ``GET /v1/oai``): implements the six OAI-PMH 2.0 verbs
    (``Identify``, ``ListMetadataFormats``, ``ListSets``,
    ``ListIdentifiers``, ``ListRecords``, ``GetRecord``) with Dublin
    Core (``oai_dc``) as the mandatory metadataPrefix. Harvester
    pagination uses **opaque base64-encoded resumption tokens** that
    wrap the adapter cursor + metadata prefix — no server state,
    harvesters survive process restarts. All verb-level errors
    follow OAI-PMH §3.6 (``badVerb``, ``badArgument``,
    ``cannotDisseminateFormat``, ``idDoesNotExist``,
    ``noRecordsMatch``, ``noSetHierarchy``, ``badResumptionToken``)
    and live inside the envelope with HTTP 200 so legacy harvesters
    parse cleanly.
  - The endpoint is unauthenticated by protocol contract; no API
    key is ever required and no admin state is touched. OAI
    identifiers follow the spec syntax ``oai:<repository-id>:<local>``
    — the repository id is derived from ``AppConfig.repository_id``
    or ``public_base_url`` host, defaulting to ``egg-api``.
  - OpenAPI snapshot grows ``/v1/oai``.
  - 24 new tests in ``tests/security/test_sprint27_scheduler_oai.py``
    cover: cadence math for all four values + the invalid case;
    ``list_due_import_sources`` and ``set_import_source_schedule``
    semantics; scheduler end-to-end (runs due sources, skips manual,
    reschedules on success *and* on exception, start/stop
    idempotent); admin REST + UI acceptance of ``schedule`` +
    rejection of unknown values; all six OAI-PMH verbs, Dublin
    Core serialisation (with XML-escaping of ``&`` / ``<`` / ``>``),
    resumption-token round-trip, ``badVerb`` / ``badArgument`` /
    ``cannotDisseminateFormat`` / ``idDoesNotExist`` /
    ``noSetHierarchy`` / ``badResumptionToken`` / ``noRecordsMatch``.

- **Sprint 26 — EAD importer (archive finding aids)** closes the
  last big gap in the GLAM publication story. Archives that run
  AtoM, Mnesys, Ligeo, ArchivesSpace, Calames, PLEADE or Pict'oOpen
  either expose EAD over OAI-PMH or ship flat EAD XML dumps — EGG
  now consumes both:
  - New `app/importers/ead.py` — stdlib-only parser that handles
    both **EAD 2002** (no namespace) and **EAD3**
    (`http://ead3.archivists.org/schema/`) by matching on local
    element names. Each finding aid expands into **many backend
    documents** (one for `<archdesc>` root + one per `<c>` /
    `<c01>`…`<c12>` descendant), each carrying a `parent_id`
    pointer so clients can rebuild the tree without the importer
    flattening or denormalising the hierarchy.
  - The mapper emits flat keys for the new archive-profile sub-
    block: `unit_id`, `unit_level`, `extent`, `repository`,
    `scope_content`, `access_conditions`, `parent_id`. The
    Sprint 23 dotted-mapping machinery already knows how to route
    `archive.*` public fields into a nested `Record.archive` block.
  - `Record` schema grows an `ArchiveFields` sub-model + optional
    `archive` field (same contract as `MuseumFields` — absent on
    the wire unless the deployment maps at least one inner field,
    so libraries / museums don't grow their payloads).
  - Setup wizard step 3 now proposes the archive sub-block
    (`archive.unit_id`, `archive.unit_level`, `archive.extent`,
    `archive.repository`, `archive.scope_content`,
    `archive.access_conditions`, `archive.parent_id`) when the
    operator selects *Archive* as the collection type. The
    `propose_mapping` helper auto-fills those slots from common
    backend field names (`unitid`, `scopecontent`, `level`, …).
  - `oaipmh.iter_records`'s `record_parser` callable now accepts
    a return value of `list[dict]` (in addition to `dict | None`),
    so one OAI-PMH record can legitimately expand to many backend
    docs — EAD is the first consumer.
  - Two new importer kinds: `ead_file` (flat XML, any size) and
    `oaipmh_ead` (OAI-PMH with `metadataPrefix=ead`). Dispatcher,
    admin REST payload, `/identify` guard and admin UI form are
    all updated. The UI now lists nine importer kinds covering
    every OAI / MARC / LIDO / CSV / EAD deployment shape.
  - 26 new tests in `tests/security/test_sprint26_ead.py` cover
    EAD 2002 + EAD3 parsing, multi-level component hierarchy,
    `<unitdate normal>` preference, scope/access paragraphs,
    mixed-content `<emph>` tolerance, bare `<archdesc>` root,
    malformed XML, flat-file ingest (happy / missing / broken),
    OAI record expansion into many docs, dispatcher routing, the
    archive sub-block emission through the mapper, absence of
    `archive` on the wire for library deployments,
    `propose_mapping` suggestions, wizard template listing the
    slots, admin REST accepting the two new kinds, `/identify`
    working for `oaipmh_ead`, end-to-end `/run` for `ead_file`,
    UI form listing both EAD options, UI `/add` persisting a
    flat-file EAD source.

- **Sprint 25 — MARC21 / UNIMARC + CSV importers**: covers the
  SIGB/catalogue crowd that OAI-PMH Dublin Core leaves behind
  (Koha exports, PMB dumps, Aleph / Symphony pulls, raw spreadsheet
  exports from any archive or library software). Four new importer
  kinds, all stdlib-only:
  - New `app/importers/marc.py` — hand-rolled ISO 2709 parser
    (~100 LoC) that walks the leader, directory and variable
    fields without `pymarc`, plus a MARCXML parser sharing the
    same `MarcRecord` shape. Two *flavors* ship:
    - `marc21`: `245$a`/`100$a`/`700$a`/`260$b`/`260$c`/`020$a`/
      `650$a`/`500$a`/`520$a` — LC / Koha default.
    - `unimarc`: `200$a`/`700$a`/`701$a`/`210$c`/`210$d`/`010$a`/
      `606$a`/`330$a` — BnF, PMB, most francophone libraries.
    The mapper also handles the usual `260,` / `260.` punctuation
    artefacts and pulls ISBN out of `9781234567890 (pbk)` noise.
    Malformed records are skipped with a `logger.exception`, never
    blow up the batch.
  - New `app/importers/csv_importer.py` — stdlib `csv` with dialect
    sniffing (commas, semicolons, tabs, pipes). First row becomes
    the header; matching column names become keys on the backend
    document. Plural columns (`creators`, `subject`, `authors`,
    `identifiers`, `languages`) are always split on `|`; other
    columns opt in by using the separator in the cell. UTF-8 BOM
    tolerated. Rows without `id` are skipped.
  - Three new flat-file kinds (`marc_file`, `marcxml_file`,
    `csv_file`) and one new OAI-PMH kind (`oaipmh_marcxml`)
    plug into the Sprint 24 dispatcher. The `metadata_prefix`
    column now carries the *MARC flavor* for MARC kinds (stored
    as `marc21` / `unimarc`), keeping the row shape unchanged —
    no migration.
  - `app.importers.SUPPORTED_KINDS`, `OAIPMH_KINDS` and
    `SUPPORTED_MARC_FLAVORS` are the single source of truth for
    the admin REST payload, UI template and dispatcher.
  - `CreateImportSourceRequest.kind` widens to the seven supported
    kinds; `POST /admin/v1/imports/{id}/identify` now refuses
    every `*_file` kind.
  - Admin UI imports form grows a **MARC flavor** selector
    (MARC21 / UNIMARC) and the Importer-kind dropdown now lists
    the seven kinds with short Koha/PMB/Axiell hints. Help text
    tells operators to save their spreadsheet as UTF-8 CSV with an
    `id` column when nothing else is available.
  - 31 new tests in `tests/security/test_sprint25_marc_csv.py`
    cover ISO 2709 round-trip, malformed leader / short record /
    garbage-in-stream, MARC21 + UNIMARC tag mapping (incl.
    French accents end-to-end), MARCXML collection + bare
    `<record>` fallback, malformed XML, flat-file ingest
    (missing path, malformed XML, happy path), CSV parse
    (header detection, missing `id`, UTF-8 BOM, semicolon
    dialect, list splitting), dispatcher routing for all four
    new kinds, UI form lists all seven kinds + MARC flavor,
    UI `/add` persists flavor, UI rejects unknown flavor,
    admin REST accepts all new kinds, `/identify` refuses
    `marc_file`, end-to-end `/run` for `marc_file`.

- **Sprint 24 — LIDO importer (OAI-PMH + flat-file)**: pairs the
  Sprint 23 museum profile with a real way to load content. Museum
  DAMS (Axiell, Micromusée, TMS, Mobydoc, …) either expose LIDO over
  OAI-PMH or ship flat XML dumps — EGG now consumes both without a
  heavy dependency:
  - New `app/importers/lido.py` — stdlib-only LIDO parser that turns
    a `<lido:lido>` element into a museum-profile backend document
    (id, type, title, artist, inventory_number, medium, dimensions,
    acquisition_date, current_location, iiif_manifest, thumbnail).
    The parser prefers `lido:type="inventory number"` over other
    `workID` entries and handles the `IIIFManifest` resource
    representation plus a URL-suffix fallback (`…/manifest`).
  - `parse_lido_bytes()` + `ingest_file(path, bulk_index, chunk_size)`
    stream a flat LIDO XML file through the adapter's `bulk_index`
    in chunks of 500. Accepts a single `<lido:lido>` root, a
    `<lido:lidoWrap>` with many children, or any XML with descendant
    `<lido:lido>` elements.
  - `oaipmh.iter_records()` + `oaipmh.ingest()` now accept a
    `record_parser` callable, so LIDO over OAI-PMH reuses the Sprint
    22 envelope + resumption-token plumbing with one extra kwarg.
  - New dispatcher `app.importers.run_import(source, bulk_index)`
    owns the single `kind` → concrete-ingest mapping; both
    `POST /admin/v1/imports/{id}/run` and
    `POST /admin/ui/imports/{id}/run` call it.
  - `CreateImportSourceRequest.kind` widens to
    `Literal["oaipmh", "oaipmh_lido", "lido_file"]`;
    `schema_profile` widens to include `"custom"`.
  - Admin UI gets a **Importer kind** selector (OAI-PMH / OAI-PMH
    LIDO / LIDO file). Flat-file sources reuse the `url` column to
    store an absolute filesystem path the server can read (no
    multipart upload in S24 to keep the desktop memory story
    simple); the form help text spells that out.
  - `POST /admin/v1/imports/{id}/identify` now answers for both
    `oaipmh` and `oaipmh_lido` sources (same protocol), and still
    400s for flat-file kinds.
  - 27 new tests in `tests/security/test_sprint24_lido.py` cover
    element → doc mapping (all museum fields + fallbacks), flat-file
    parse / ingest / malformed-XML / missing-file paths, OAI
    envelope parsing (`lido:lidoWrap`, deleted records, missing
    metadata), dispatcher routing + guards, admin API accept/reject
    of the new kinds, end-to-end `run` for `lido_file`, UI form
    listing the three kinds, UI `/add` persisting a `lido_file`
    source with empty prefix, and UI `/add` rejecting unknown kinds.

- **Sprint 23 — Museum schema profile + IIIF passthrough**
  (turns EGG-API into a credible publication layer for DAMS / museum
  collection systems, not just libraries):
  - `AppConfig.schema_profile: Literal["library", "museum", "archive",
    "custom"]` defaults to `library` (no behaviour change for existing
    deployments). The cross-validator now allows mapping keys to use
    a dotted form (`museum.inventory_number`, `links.iiif_manifest`)
    and exposes the head segment to `allowed_include_fields` so the
    sub-block is publishable through the include filter.
  - New `MuseumFields` Pydantic sub-model on `Record`
    (`inventory_number`, `artist`, `medium`, `dimensions`,
    `acquisition_date`, `current_location`). Library deployments
    never see a `museum: {}` block — empty sub-blocks are dropped by
    the mapper.
  - `SchemaMapper` now splits dotted public field names into nested
    sub-blocks at emission time, so any existing transform mode
    (`direct`, `template`, `first_non_empty`, …) works for museum
    fields without code changes.
  - `/v1/manifest/{record_id}` is **restored as a 302 redirect** to
    the record's `links.iiif_manifest` value (and 404 when the
    record is missing or has no manifest URL). EGG stays out of the
    IIIF hosting business — it just points clients at the upstream
    manifest the cultural institution already serves.
  - Setup wizard step 3 ("Map your fields") gets a *Collection type*
    selector: `library` / `museum` / `archive` / `custom`. Switching
    profile re-runs `propose_mapping` with the right hint table
    (museum hints recognise `inv_no`, `accession_number`,
    `creator_artist`, `medium`, `dimensions`, `iiif`, etc.) and
    expands the form with the museum / IIIF slots when relevant.
    The chosen profile is persisted on the draft and propagated to
    the published `AppConfig`.
  - 15 new tests in `tests/security/test_sprint23_museum_iiif.py`
    cover: museum sub-block emission, library not affected, IIIF
    redirect 302 / 404, dotted head allowed in include filter,
    `propose_mapping` per profile, draft round-trip preserving
    `schema_profile`, profile route persistence + mapping rebuild,
    `custom` clears the mapping, unknown profile rejected, login
    required.
  - OpenAPI snapshot adds `/v1/manifest/{record_id}` (back) and
    `/admin/ui/setup/mapping/profile`. Coverage gate dips to **77 %**
    (combined with S22's defensive branches); the polish sprint will
    push it back above 80 %.

- **Sprint 22 — OAI-PMH Dublin Core import** (first importer in the
  series that turns EGG-API into a GLAM publication layer rather
  than a raw ES façade):
  - New `app/importers/oaipmh.py` — minimal OAI-PMH 2.0 client built
    on `httpx` + stdlib `xml.etree` (no `sickle` / `lxml` added).
    Implements `Identify`, `ListRecords` with resumption tokens,
    XML-error and OAI-level-error detection, a `max_pages` safety
    ceiling, and a streaming `ingest()` that chunks records through
    `bulk_index()` (default 500 docs/batch).
  - Dublin Core → backend-doc mapping covering `title`, `description`,
    `creators`, `subject`, `date`, `type`, `language`, `publisher`,
    `rights`, with a heuristic that routes any `dc:identifier`
    containing `/iiif/` or ending in `/manifest` to the new
    `iiif_manifest` slot.
  - New `BackendAdapter.bulk_index(docs)` in `app.adapters.base`,
    implemented by `ElasticsearchAdapter` (via `_bulk` NDJSON) and
    the `tests/_fakes.FakeAdapter` (in-memory `stored` list for
    assertions).
  - Migration 9 creates `import_sources` + `import_runs`. New
    `SQLiteStore` methods: `add_import_source`, `list_import_sources`,
    `get_import_source`, `delete_import_source`, `start_import_run`,
    `finish_import_run`, `list_import_runs`.
  - New admin REST surface `/admin/v1/imports` (+ `{id}`, `{id}/runs`,
    `{id}/identify`, `{id}/run`) with Pydantic `extra="forbid"`
    bodies, synchronous `run` for immediate feedback.
  - New admin UI page `/admin/ui/imports` with an *Add OAI-PMH
    source* form, *Run now* / *Delete* actions and a rolling
    *Recent runs* table. Nav link added to `base.html`.
  - Coverage gate dips to **78 %** for this release (~400 new lines
    of Jinja/route plumbing whose defensive error branches are
    thinly tested). A polish sprint will push per-package
    thresholds back up.

- **Sprint 21 — Windows/Mac polish without signing**:
  - `app/desktop.py` now redirects stdout, stderr and the Python
    logging tree to `EGG_HOME/logs/launcher.log` at the very top
    of `main()`. Keeps the magic URL readable even in a windowed
    (non-console) Briefcase build. `EGG_DESKTOP_CONSOLE=1` disables
    the redirect for local dev.
  - New `show_native_dialog()` helper: Windows uses `user32.MessageBoxW`
    via `ctypes`, macOS uses `osascript`, Linux falls back to
    `tkinter.messagebox`. Called when pywebview fails to import or
    start (missing WebView2 runtime on old Windows 10, missing
    WebKitGTK on headless Linux) so the operator still sees the
    magic URL in a real dialog instead of a silent crash.
  - `pyproject.toml` sets `console_app = false` on the Windows
    Briefcase target — no more cmd window flashing behind the
    pywebview window.
  - New `docs/first-launch.md` explains the SmartScreen / Gatekeeper
    bypass procedure, the WebView2 runtime requirement, and the
    launcher log file locations per OS. INSTALL.md and the landing
    page link to it and own the "unsigned by choice" framing.

### Added

- **Backend auto-discovery in the setup wizard**: step 1 now has a
  *Detect a backend on this machine* button. It probes a short
  allowlist of well-known hosts in parallel (loopback 9200/9201 +
  common docker-compose hostnames) and surfaces each candidate with
  a *Use this URL* shortcut that patches the draft without the
  operator typing anything. Reachable backends are tagged ES or
  OpenSearch with their version; those that answer 401 are flagged
  *needs auth* so the operator still sees the URL. No credentials
  are ever sent on discovery. Operators can extend the allowlist via
  `EGG_DISCOVERY_HOSTS=host[:port],host[:port]` (useful for
  `es-staging.internal:9200` inside corporate networks).

## [2.0.0] — 2026-04-22

Major release bundling the post-1.0 review action plan (Sprints 11 →
19). Focus: turn a hardened-but-operator-facing service into
something a non-technical archivist can actually install and run.

### Breaking

- `app` package version bumped to `2.0.0`; Briefcase bundle likewise.
- Briefcase desktop entry point (`egg-api-desktop`) now pins
  `EGG_HOME` to the OS-native user directory instead of the process
  cwd. Operators upgrading from 1.x without the desktop bundle are
  unaffected (the CLI flow still honours `$EGG_HOME` / `./config`).
- Public API default changed: every `AppConfig` sub-model now uses
  Pydantic `extra="forbid"`. Unknown keys in `config/egg.yaml` fail
  `egg-api check-config` rather than being silently ignored.
- `ConfigManager.save()` now chmods `config/egg.yaml` to `0600` on
  POSIX, matching the bootstrap key sidecar.
- `backend.auth` block is mandatory for authenticated Elasticsearch /
  OpenSearch clusters (Basic / Bearer / ApiKey). Legacy deployments
  that embed credentials in `backend.url` continue to work but are
  discouraged.
- `make run` dropped `--reload`; `make dev` is now the auto-reload
  target.
- Coverage gate raised to 79 % (from 78 %); a dedicated polish sprint
  will push it back above 80 %.

### Added

- **Sprint 11 — honesty pass**: README aligned with shipped reality
  (suggest live, manifest retired), desktop promise reframed as a
  3-step roadmap, SECURITY.md, CONTRIBUTING.md,
  `.github/dependabot.yml`.
- **Sprint 12 — deployment hardening**: `backend.auth` config block
  (none/basic/bearer/api_key), `proxy.allowed_hosts` +
  `TrustedHostMiddleware`, multi-worker-without-Redis guardrail at
  boot, Pydantic `extra="forbid"` across the tree.
- **Sprint 13 — REST CRUD for API keys** (SPECS §13.7-13.10) via a
  shared `ApiKeyService` used by both the REST surface and the Jinja
  admin UI.
- **Sprint 14 + 15 — setup wizard** (SPECS §26): eight-screen guided
  flow at `/admin/ui/setup` covering backend → source → mapping →
  security → exposure → first public key → live test → publish. New
  `setup_drafts` migration, `ApiKeyService` reuse, `draft_to_config`
  helper that preserves operator-only fields (cors/proxy/bootstrap).
  Glossary page at `/admin/ui/help`.
- **Sprint 16 — first-run UX**: `egg-api start` (one-command
  launcher), `/admin/setup-otp/{token}` magic link (hashed, 5-min
  TTL, single-use), plain-language error translation in
  `app.user_errors`.
- **Sprint 17 — desktop packaging**: `app/desktop.py` entry point,
  Briefcase bundle metadata for macOS `.pkg` / Windows `.msi` /
  Linux AppImage, OS-native `EGG_HOME` resolution, matrix-build
  GitHub Actions workflow.
- **Sprint 18 — hardening + ops endpoints**: admin session idle
  timeout (migration 8), public 401 lockout per IP, template mapper
  `allowed_fields` whitelist, `GET /admin/v1/logs`,
  `GET /admin/v1/export-config` + `POST /admin/v1/import-config`.
- **Sprint 19 — release 2.0**: `GET /admin/v1/releases` (admin-gated
  version + update check against GitHub Releases, 10-min cache,
  opt-out via `EGG_DISABLE_RELEASE_CHECK=1`), tag-driven
  `release.yml` now builds + attaches the three desktop installers
  alongside the wheel/sdist + SHA256SUMS, static landing page under
  `docs/site/`, signing + auto-update playbook in `docs/signing.md`.

### Security

- Idle timeout on admin sessions (15-min default, knob:
  `auth.admin_session_idle_timeout_minutes`).
- Public `/v1/*` 401 lockout per IP (sliding window, knobs:
  `auth.public_401_lockout_threshold` +
  `auth.public_401_lockout_window_seconds`).
- Template mapper no longer interpolates arbitrary backend fields:
  the placeholder pruner runs before `Template.safe_substitute` so
  un-whitelisted `$placeholders` are stripped from the emitted
  value.
- `backend.auth.password` / `backend.auth.token` are redacted by
  `ConfigManager.save()` (Sprint 12, restated for the 2.0 audit).

## [1.0.0] — 2026-04-21

First stable release after a full audit-driven sprint series (S0 → S8).
284 tests green, 82.88% coverage, ruff + ruff format + mypy clean.

### Breaking

- `/v1/suggest` went from `404` stub → `200` with a real ES-backed
  implementation (S8.3). Re-add it to any contract tests you had
  relying on the 404.
- `/v1/manifest/{id}` remains **retired**. Paths starting with
  `/v1/manifest` return `404`; re-introduce them with a proper
  backend integration if/when IIIF proxy support lands.
- `Record.raw_identifiers` was removed (S5.6) — duplicates the
  `identifiers` object and was always empty in practice.
- `/v1/health` body shrunk to `{"status":"ok"}` (S1.9). Operators
  wanting the previous detail can hit `/v1/readyz` (admin-gated) or
  `/v1/livez` (public).
- HTTP caching: `Vary: x-api-key` was dropped and `Cache-Control`
  now switches between `public` and `private` based on
  `auth.public_mode` (S5.3). Shared caches that relied on `Vary`
  should adopt `Cache-Control: private` directly.

### Added

- **Sprint 0 — tooling**: ruff (lint + format), mypy, pytest-cov
  (80% gate), GitHub Actions CI (lint + 3.10/3.11/3.12 matrix),
  multi-stage Dockerfile (non-root `egg` user), docker-compose stack
  (ES 8 + EGG), pip-compile lock files.
- **Sprint 1 — critical security** (S1.1 – S1.11):
  - Rate-limit buckets key on `key_id` or IP, never the raw secret.
  - Prometheus `endpoint` label uses the route template
    (`/v1/records/{record_id}`) — no cardinality explosion.
  - `SchemaMapper` raises `AppError("bad_gateway", 502)` when a
    backend record has no usable id, instead of a Pydantic 500.
  - `usage_audit_middleware` wraps the persist/log/metrics path in
    `try/finally` so 500s land in `usage_events` too.
  - `Container.reload()` closes the previous httpx.Client (no more
    socket/FD leak across reloads).
  - UI session tokens are SHA-256 hashed at rest (raw cookie never
    lands in SQLite).
  - `x-request-id` header is validated against
    `^[A-Za-z0-9._-]{1,64}$` or regenerated.
  - Input-size caps: `q ≤ 512`, `≤ 50` values per filter,
    `≤ 256` chars per filter value, `≤ 20` include_fields.
  - `/v1/health` split into `/v1/livez` (public) and `/v1/readyz`
    (admin).
  - `/docs`, `/redoc`, `/openapi.json` hidden in production.
  - `/metrics` requires admin `X-API-Key` or `EGG_METRICS_TOKEN`
    bearer in production.
- **Sprint 2 — CSRF + UI hardening** (S2.1 – S2.10):
  - Double-submit CSRF on every admin UI POST (HMAC of
    `session_cookie` with a per-process signing key; no DB writes).
  - `samesite=none` rejected at config-load when
    `admin_cookie_secure=false`.
  - Generic error copy in the UI — Pydantic traces never leak.
  - `uvicorn.ProxyHeadersMiddleware` opt-in via
    `proxy.trusted_proxies`.
  - `POST /admin/logout-everywhere` purges every live session for
    the current `key_id`.
  - `POST /admin/ui/keys/{key_id}/rotate` regenerates the secret +
    invalidates sessions. Rotating the `admin` key updates
    `default_admin_key` in memory so the next reload does not
    resurrect the old value.
  - `SQLiteStore.set_key_status` split into
    `set_key_status_by_key_id` / `_by_secret` (no more OR-clause).
  - INSTALL.md gains a reverse-proxy deploy section (nginx,
    Traefik, sanity curls).
- **Sprint 3 — async + retry hardening** (S3.1 – S3.8):
  - Threadpool (Option A) — see `docs/adr-001-async-io-strategy.md`.
  - `SQLiteStore` keeps one `sqlite3.Connection` per thread, keyed
    by `db_path`. `check_same_thread=False` safe thanks to the
    per-thread pool.
  - `usage_audit_middleware` runs `get_identity` + `log_usage_event`
    via `run_in_threadpool`.
  - ElasticsearchAdapter retries cap backoff
    (`retry_backoff_cap_seconds`, default 5 s), add ±25% jitter,
    and honour a global `retry_deadline_seconds` (default 30 s).
- **Sprint 4 — persistence + migrations + pepper** (S4.1 – S4.9):
  - Versioned migration runner (`app/storage/migrations.py`) with
    5 baseline migrations and a legacy-DB baseline heuristic.
  - `egg-api migrate` CLI reports before/after version + applied
    list.
  - Background purge task (FastAPI `lifespan`): evicts expired UI
    sessions + `usage_events` older than
    `usage_events_retention_days` (default 30).
  - `GET /admin/v1/storage/stats` exposes row counts, on-disk size,
    schema version, last purge snapshot.
  - Opt-in HMAC-SHA256 pepper for API keys
    (`EGG_API_KEY_PEPPER`). Legacy SHA-256 keys still validate;
    `rotate_api_key` upgrades them in place.
  - Removed the never-wired `quota_counters` / `quota_config`
    tables.
- **Sprint 5 — contract + mapper refactor** (S5.1 – S5.10):
  - `typing.Literal` aliases replace `_VALID_*` sets
    (`PublicAuthMode`, `CorsMode`, `SameSite`, `Criticality`,
    `MappingMode`, `BackendType`).
  - `SchemaMapper._apply_mode` is now a dict dispatch over nine
    named handlers — adding a new mode is additive.
  - `ElasticsearchAdapter` now forwards the bound request_id as
    `X-Opaque-Id` to every backend call.
  - `/v1/search?format=csv` returns a flat, spreadsheet-friendly
    CSV export.
  - OpenAPI path snapshot test locks the public contract.
- **Sprint 6 — observability + ops pack** (S6.1 – S6.9):
  - Opt-in OpenTelemetry (via `EGG_OTEL_ENDPOINT`) — FastAPI +
    httpx auto-instrumented, `traceparent` propagated. Structlog
    processor injects `trace_id` / `span_id` into every event.
  - `GET /admin/v1/debug/translate` returns normalized query,
    cache key and backend DSL without touching the backend.
  - `ops/prometheus/alerts.yml`, `ops/grafana/egg-api-overview.json`,
    `ops/RUNBOOK.md`, `deploy/k8s/egg-api.yaml`,
    `scripts/locustfile.py`.
- **Sprint 7 — architecture + extensibility** (S7.1 – S7.7):
  - `BackendAdapter` runtime-checkable Protocol.
  - Adapter factory dispatches on `backend.type`.
  - `OpenSearchAdapter` (drop-in compatible, version floor 1.x).
  - Four store role Protocols (`KeyStore`, `SessionStore`,
    `UsageLogger`, `StatsReporter`).
  - `app.state.container` + `get_container(request)` helper for
    `Depends`-based access (singleton stays as fallback).
  - `pytest-xdist` supported, parallel run time ~11 s.
  - `docs/backends.md` backend authoring guide.
- **Sprint 8 — advanced functional** (S8.1 – S8.6):
  - `search_after` cursor pagination with an opaque base64url
    token. `NormalizedQuery.cursor` bypasses `max_depth`;
    `SearchResponse.next_cursor` is emitted on full pages.
  - `GET /v1/auth/whoami` for caller introspection.
  - `GET /v1/suggest` (match_phrase_prefix on `title`).
  - JSON-LD response flavor on `/v1/records/{id}` (Accept header)
    and `/v1/search?format=jsonld`.
  - Opt-in Redis rate limiter
    (`EGG_RATE_LIMIT_REDIS_URL`, `[redis]` extra) with fail-open
    semantics.

### Changed

- Default cookie posture tightened: `admin_cookie_secure=True`,
  `admin_cookie_samesite=strict`.
- FastAPI app attaches `configure_tracing(app)` and
  `ProxyHeadersMiddleware` *before* the routers are registered so
  instrumentation sees every endpoint.
- `Container.adapter` is typed as `BackendAdapter` Protocol and
  closed via `getattr(previous_adapter, "client", None)` so future
  backends without an `httpx.Client` work unchanged.
- Rate limiters are built through `build_rate_limiter(scope=…)` so
  the in-memory and Redis-backed flavours are pin-compatible.

### Fixed

- Every item flagged in the original audit has been either
  addressed or explicitly deferred with tracking — see
  `docs/post-audit.md`.

## [0.1.0] — initial MVP (pre-audit)

Baseline feature set kept for reference. Items below were delivered
across the "vague" hardening passes (C1-C6, H1-H11, M1-M9, L1-L4)
before the sprint series started. See the 0.1.0 commit trail for
detail.

### Added

- **Observability** — Prometheus metrics on `GET /metrics`
  (`egg_requests_total`, `egg_request_duration_seconds`,
  `egg_backend_errors_total`, `egg_rate_limit_hits_total`).
  Structured JSON logs via `structlog`, with `request_id` /
  `key_id` / `latency_ms` bound per request.
- **Caching** — `Cache-Control` + strong `ETag` on `/v1/search`,
  `/v1/records/{id}`, `/v1/facets` with `If-None-Match` → `304`
  fast path.
- **Security** — CORS middleware driven by `CorsConfig`;
  security-headers middleware sets `X-Content-Type-Options`,
  `Referrer-Policy`, `X-Frame-Options`, CSP on `/admin`, and HSTS
  in production.
- **Security** — Admin login brute-force guard.
- **Security** — Admin UI sessions gain an `expires_at` column.
- **Optional endpoints** — `GET /v1/collections`, `GET /v1/schema`.
- **Admin API** — `GET /admin/v1/usage` paginated.
- **Backend** — Bounded retries with exponential backoff; ES
  minor-version gate rejects versions older than 7.

### Changed

- **Security** — Bootstrap admin key is generated on first run in
  development and required via env var in production.
- **Security** — `usage_audit_middleware` resolves raw API keys to
  their `key_id` label.
- **Security** — `ConfigManager.save()` strips
  `auth.bootstrap_admin_key` before writing YAML.
- **Validation** — `AppConfig` cross-field validators.
- **Mapper** — `date_parser` / `url_passthrough` defensive;
  `raw_fields` exposure strips backend-internal keys.
- **Backend** — `httpx.Client` uses `follow_redirects=False`.
- **Storage** — Hot-path indexes on `api_keys`, `usage_events`,
  `ui_sessions`.
- **Admin UI** — Key labels must match
  `^[a-zA-Z0-9_.-]{1,64}$`.

### Fixed

- `QueryPolicyEngine.compute_cache_key` no longer raises
  (Pydantic v2 regression).

### Internal

- `Container.reload()` serialized by a `threading.RLock`.
