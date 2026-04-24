# Security Policy

EGG-API is aimed at small GLAM institutions (galleries, libraries, archives,
museums) that expose public data through a hardened façade. Because users may
not have in-house security staff, we take vulnerability reports seriously and
try to respond quickly.

## Supported versions

| Version | Security fixes |
| --- | --- |
| `1.0.x` | Yes |
| `< 1.0` | No — please upgrade |

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Report privately via one of:

1. GitHub → repository page → `Security` tab → *Report a vulnerability*
   (uses GitHub's private advisory flow).
2. E-mail the maintainer listed in `pyproject.toml` (PGP key on request).

Please include:

- A minimal reproducer or description of the attack path.
- The version/commit SHA you tested against.
- The impact you observed or expect (data disclosure, DoS, privilege
  escalation, etc.).
- Any suggested mitigation if you have one.

We aim to:

- Acknowledge receipt within **3 working days**.
- Provide a first impact assessment within **10 working days**.
- Ship a fix (or a documented mitigation) within **30 days** for
  high/critical issues, longer for medium/low with agreement.

## Scope

In scope:

- The `app/` runtime (FastAPI service, admin API, admin UI, adapters).
- The default configuration templates under `examples/`.
- The operator CLI (`egg-api`) and the setup scripts.
- The documented deployment paths (Docker image, reverse-proxy recipes in
  `INSTALL.md`).

Out of scope:

- Issues requiring already-compromised admin credentials to exploit
  (unless the issue is privilege escalation *beyond* admin).
- Vulnerabilities in backend engines themselves (Elasticsearch,
  OpenSearch) — please report those upstream.
- Denial-of-service from unconstrained infrastructure (e.g. running the
  service without the documented rate-limit / reverse-proxy guidance).
- Social-engineering attacks against maintainers.

## What we ask of reporters

- Please act in good faith, avoid privacy violations, and do not exfiltrate
  data beyond what is necessary to demonstrate the issue.
- Give us a reasonable window to fix before public disclosure; we are
  happy to coordinate a CVE and a public advisory.

## Hardening baseline

The current security posture is summarised in the `Security model` section
of the README and in `docs/post-audit.md`. Known gaps being worked on are
tracked under the `security` label in the issue tracker.
