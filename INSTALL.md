# Installation and Local Operations

## 1) Install

```bash
./scripts/setup.sh
```

## 2) Initialize local runtime files

```bash
egg-api init
```

## 3) Validate configuration

```bash
egg-api check-config
```

## 4) Start service

```bash
egg-api run --reload
```

## 5) Useful operations

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
```

Use `["*"]` only when the app is guaranteed unreachable except through the
proxy.

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
