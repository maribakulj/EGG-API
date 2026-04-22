# Installation and Local Operations

## Recommended first run (single command)

```bash
./scripts/setup.sh
egg-api start
```

`egg-api start` is the first-run-friendly launcher introduced in
Sprint 16. It:

1. creates `config/egg.yaml` + the state DB if they do not exist;
2. generates the bootstrap admin key and **prints it to the terminal**
   (and also stores it in `data/bootstrap_admin.key`, 0600);
3. mints a one-time magic link
   `http://127.0.0.1:8000/admin/setup-otp/<token>` that opens the
   admin UI wizard without the login form;
4. opens that URL in the default browser (pass `--no-browser` on
   headless hosts — the link is still printed to stdout);
5. drops into uvicorn so the service actually answers.

The magic link is single-use and expires after 5 minutes. A subsequent
`egg-api start` reuses the existing key and mints a fresh link.

## Manual flow (operator-oriented)

```bash
./scripts/setup.sh          # venv + editable install
egg-api init                # scaffold config + state DB
egg-api check-config        # validate egg.yaml
egg-api run                 # production-style (no --reload)
# or: make dev              # auto-reload for development
```

## Useful operations

```bash
egg-api print-paths
egg-api check-backend
```

## Admin access

- Admin UI login: `http://127.0.0.1:8000/admin/login`
- Admin API base: `http://127.0.0.1:8000/admin/v1`

## Public access

- Public API base: `http://127.0.0.1:8000/v1`
- Liveness: `GET /v1/livez`
- Readiness (admin): `GET /v1/readyz` with `X-API-Key`

## Runtime path variables

- `EGG_HOME`
- `EGG_CONFIG_PATH`
- `EGG_STATE_DB_PATH`
- `EGG_BOOTSTRAP_ADMIN_KEY`
- `EGG_METRICS_TOKEN` — bearer token required to scrape `/metrics` in production

## Stop/restart

- Stop: `Ctrl+C`
- Restart: `egg-api run`

## Constrained environments

If internet access is restricted, install from internal mirror/wheelhouse:

```bash
python -m pip install --no-index --find-links /path/to/wheels -e .[dev]
```

Then continue with `egg-api init` and `egg-api run`.

## Deploying behind a reverse proxy

EGG-API is a plain ASGI app; put it behind nginx, Traefik, Caddy, or any TLS
terminator. Two knobs must be aligned between the proxy and the app:

1. **Trusted-proxy list**: enables rewriting `request.client.host` and
   `request.url.scheme` from `X-Forwarded-For` / `Forwarded` headers so that
   rate limiting and audit logs attribute traffic to the real client, not
   the proxy loopback.
2. **HTTPS**: with `EGG_ENV=production`, HSTS and secure cookies rely on the
   request reaching the app as HTTPS. `ProxyHeadersMiddleware` needs to see
   `X-Forwarded-Proto: https` from a trusted hop.

### Config

Add a `proxy` block to `config/egg.yaml`:

```yaml
proxy:
  trusted_proxies:
    - 127.0.0.1
    - 10.0.0.0/8
  allowed_hosts:
    - egg.example.org
```

- `trusted_proxies` gates the `X-Forwarded-*` rewrite. Use `["*"]`
  only when the app is guaranteed unreachable except through the proxy.
- `allowed_hosts` is applied by `TrustedHostMiddleware`: any request
  whose `Host` header does not match is rejected with `400` before the
  handler runs. Leave empty for local dev; pin it in production to the
  names the service is actually advertised under (wildcards supported,
  e.g. `*.example.org`).

### Backend authentication

Elasticsearch 8+ ships with security enabled by default. Configure the
adapter's credentials under `backend.auth` — never in `backend.url`:

```yaml
backend:
  url: https://elasticsearch.internal:9200
  auth:
    mode: basic          # none | basic | bearer | api_key
    username: egg_ro
    password_env: EGG_BACKEND_PASSWORD   # secret read from env at boot
```

Inline `password` / `token` values are accepted in-memory (so a
round-trip through `PUT /admin/v1/config` works) but are stripped by
`ConfigManager.save()` so the on-disk YAML never contains the secret.
The `*_env` indirection is the recommended form.

### Multi-worker deployments

The default rate limiter is **per-process**. Running with more than one
worker without a shared backend silently multiplies the published
public rate limit by the worker count. EGG-API therefore:

- refuses to start in production (`EGG_ENV=production`) when
  `EGG_WORKERS` / `WEB_CONCURRENCY` / `UVICORN_WORKERS > 1` and
  `EGG_RATE_LIMIT_REDIS_URL` is unset;
- prints a loud warning in development in the same situation.

Set `EGG_RATE_LIMIT_REDIS_URL=redis://...` to share state across
workers, or keep the default single-worker run.

### Example: nginx

```nginx
upstream egg {
  server 127.0.0.1:8000;
}

server {
  listen 443 ssl http2;
  server_name egg.example.org;

  ssl_certificate     /etc/ssl/egg.crt;
  ssl_certificate_key /etc/ssl/egg.key;

  # Force HTTPS; EGG emits HSTS with EGG_ENV=production.
  add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

  location / {
    proxy_pass http://egg;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Host  $host;
    proxy_read_timeout 30s;
  }
}

server {
  listen 80;
  server_name egg.example.org;
  return 301 https://$host$request_uri;
}
```

### Example: Traefik (static labels)

```yaml
labels:
  - traefik.enable=true
  - traefik.http.routers.egg.rule=Host(`egg.example.org`)
  - traefik.http.routers.egg.entrypoints=websecure
  - traefik.http.routers.egg.tls=true
  - traefik.http.services.egg.loadbalancer.server.port=8000
  # Ensure Traefik forwards the standard X-Forwarded-* headers (default on).
```

### Sanity check

After deploying, from an external client:

```bash
# Public liveness should be reachable without a key.
curl -sfI https://egg.example.org/v1/livez

# /metrics should refuse anonymous scrapes in prod.
curl -sI https://egg.example.org/metrics               # expect 401
curl -sI https://egg.example.org/metrics \
     -H "Authorization: Bearer $EGG_METRICS_TOKEN"     # expect 200

# /docs should be hidden (404) in prod.
curl -sI https://egg.example.org/docs                  # expect 404
```

Audit logs (look for `"route":"/v1/..."` entries) should carry the real
client IP in their `subject` / rate-limit bucket. If they still report the
proxy IP, `proxy.trusted_proxies` is not picking up the hop.
