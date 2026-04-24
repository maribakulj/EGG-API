# Contributing to EGG-API

Thanks for taking the time to help. EGG-API is a small project with a
sharp scope (a safe public façade for GLAM backends), so a few ground
rules make contributions merge smoothly.

## Before you start

- Read the **SPECS.md** — it is the product source of truth (in French).
  Behaviour changes must stay consistent with it, or update it in the
  same PR.
- Run the tests once on `main` to make sure your environment is good:

  ```bash
  ./scripts/setup.sh
  make test
  ```

- For anything bigger than a one-liner, **open an issue first** so we can
  agree on the approach. This avoids wasted work on a PR that ends up
  refactored or rejected.

## Development loop

```bash
make setup          # create venv + editable install with dev extras
make init           # scaffold config + state DB + bootstrap admin key
make dev            # uvicorn with --reload (development only)
make test           # pytest with coverage gate (>= 80%)
```

Useful extras:

```bash
ruff check .        # lint
ruff format .       # format
mypy app            # type-check (partial strictness)
pre-commit run -a   # everything above at once
```

## Pull request checklist

- [ ] Branch name describes the change (`feat/...`, `fix/...`,
      `docs/...`, `chore/...`).
- [ ] `make test` passes locally. Coverage gate is **80%**.
- [ ] New behaviour is covered by at least one test in the appropriate
      folder (`tests/unit/`, `tests/integration/`, `tests/security/`,
      `tests/contract/`).
- [ ] Public API shape changes touch the contract test in
      `tests/contract/test_contract.py`.
- [ ] Configuration changes are reflected in `examples/config.yaml`
      *and* in the Pydantic models under `app/config/`.
- [ ] User-visible changes are listed in `CHANGELOG.md` under
      *Unreleased*.
- [ ] Security-sensitive changes: add a note to the PR description
      flagging the threat model impact, and if applicable a regression
      test under `tests/security/`.

## What we avoid

- **Silent spec drift.** If code behaviour diverges from SPECS.md or
  README.md, fix the doc or the code in the same PR. Never both out of
  sync.
- **Feature-flag graveyards.** Prefer a clean toggle through `AppConfig`
  with validation, not ad-hoc `os.getenv` sprinkled in handlers.
- **Unbounded labels in metrics.** Prometheus labels must use the route
  template (`/v1/records/{id}`), never the raw path.
- **Raw secrets in YAML.** The admin bootstrap key is the only secret we
  persist, and only in the 0600 sidecar file. Do not add new YAML-backed
  secret fields; add an environment variable with a helper in
  `app/runtime_paths.py`.
- **New dependencies** without justification. The runtime dep set is
  kept tight on purpose — open an issue if you think we need one.

## Testing guidelines

- Tests must run **offline**. Real backend calls are banned; use the
  `FakeAdapter` in `tests/_fakes.py` or `httpx.MockTransport`.
- Keep a hard separation:
  - `tests/unit/` — pure functions, no FastAPI app.
  - `tests/integration/` — HTTP client hitting the app.
  - `tests/security/` — regression suites for audit findings; each
    test maps to a finding ID in `docs/post-audit.md` where relevant.
  - `tests/contract/` — public API shape; any change here implies a
    bumped response schema.

## Security issues

Please see [`SECURITY.md`](./SECURITY.md). Do **not** file security
problems as public issues or PRs.

## License

By contributing, you agree that your contribution is licensed under the
project's MIT license (see `LICENSE`).
