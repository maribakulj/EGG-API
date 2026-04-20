# EGG-API Operator Runbook

Triage order for common incidents. Every section assumes Prometheus + the
provided alerts (`ops/prometheus/alerts.yml`) and at least structured-log
access to the process (`EGG_LOG_LEVEL=INFO` at minimum).

## Quick reference

| Signal                               | Likely section                      |
| ------------------------------------ | ----------------------------------- |
| `EGGApiDown` firing                  | [§process-down](#process-down)      |
| `EGGBackendUnavailable` firing       | [§es-outage](#es-outage)            |
| `EGGApiHighErrorRate` firing         | [§high-5xx](#high-5xx)              |
| `EGGApiP99Slow` firing               | [§slow-backend](#slow-backend)      |
| `EGGRateLimitBurst` firing           | [§rate-limit-burst](#rate-limit-burst) |
| "my admin UI shows CSRF check failed" | [§csrf-mismatch](#csrf-mismatch)   |
| Lost the bootstrap admin key         | [§key-recovery](#key-recovery)      |
| Retention looks stuck                | [§purge](#purge)                    |

---

## process-down

Prometheus cannot scrape `/metrics` for ≥ 2 min.

1. Check liveness from a trusted host:
   ```bash
   curl -fsS https://egg.example.org/v1/livez
   ```
   200 + `{"status":"ok"}` means the process is up; the problem is between
   Prometheus and the pod (service / ingress / network policy).
2. If liveness fails:
   ```bash
   kubectl logs deploy/egg-api --tail=200
   egg-api check-config
   egg-api check-backend
   ```
3. If the process is crash-looping at startup, the most common causes are:
   - `EGG_ENV=production` without `EGG_BOOTSTRAP_ADMIN_KEY` → refuses to start.
   - `config.yaml` failed validation → Pydantic error in the logs.
   - SQLite migration failing → run `egg-api migrate` out-of-process with
     the same `EGG_STATE_DB_PATH` and inspect the JSON output.

## es-outage

`egg_backend_errors_total{error_code="backend_unavailable"}` rising.

1. Check ES directly:
   ```bash
   curl -fsS "$EGG_BACKEND_URL/_cluster/health"
   ```
2. If ES is green/yellow, the issue is EGG→ES networking (firewall, DNS).
3. If ES is unreachable, restore it first: EGG retries honor
   `backend.retry_deadline_seconds` (default 30 s) and surface a typed 503
   to clients in the meantime.
4. Once ES is back, latency should return in one retry cycle. No EGG
   restart needed.

## high-5xx

5xx share > 5% for 10+ min.

1. Group by route/status in logs:
   ```jq
   kubectl logs deploy/egg-api --since=10m | \
     jq -r 'select(.status_code >= 500) | [.route, .status_code, .error_code] | @tsv' \
     | sort | uniq -c | sort -rn | head
   ```
2. If `error_code="unhandled_exception"` dominates, a handler regression
   landed recently; consider rolling back.
3. If `error_code="backend_unavailable"`, go to [§es-outage](#es-outage).
4. If `error_code="bad_gateway"`, the backend is returning malformed
   records (missing `id`/`_id`). Check the ES index's ingest pipeline.

## slow-backend

p99 on `/v1/search` > 1 s.

1. Pull the slowest requests from the logs:
   ```jq
   jq 'select(.latency_ms > 1000) | {route, latency_ms, trace_id, request_id}'
   ```
2. If OTel is wired (`EGG_OTEL_ENDPOINT`), pivot on the `trace_id` in your
   tracing UI — the ES call is a child span, so you see the real backend
   time split from EGG overhead.
3. Without tracing: ES slow logs honor `X-Opaque-Id` (= EGG `request_id`),
   grep your ES cluster for the request_id to confirm.
4. Common root causes: large `page_size`, deep pagination near `max_depth`,
   broad facet requests over unindexed fields.

## rate-limit-burst

`egg_rate_limit_hits_total` above 5 req/s.

1. Identify the subject:
   ```sql
   SELECT api_key_id, subject, COUNT(*)
   FROM usage_events
   WHERE status_code = 429 AND timestamp > datetime('now', '-1 hour')
   GROUP BY api_key_id, subject
   ORDER BY 3 DESC;
   ```
2. If legitimate: raise `rate_limit.public_max_requests` via the admin UI
   or API and reload.
3. If abusive: suspend the key from the admin UI (Keys → Suspend) or the
   CLI (`ApiKeyManager.suspend_by_key_id`).
4. For IP-scoped bursts (no API key), widen the reverse-proxy ban list or
   add an L4 ACL — EGG has no IP banlist of its own yet.

## csrf-mismatch

"CSRF check failed. Reload the page and retry." on any admin UI POST.

1. Most common cause: the server was restarted (new CSRF signing key).
   Solution: reload the page — a fresh token is issued on the next GET.
2. Second cause: `admin_cookie_samesite: lax` + cross-origin POST. Either
   switch back to `strict` or ensure the admin UI is only accessed
   same-origin.
3. If it persists after a hard reload, inspect the cookie jar: a stale
   `egg_admin_session` for a deleted session will fail silently. Sign out
   and back in.

## key-recovery

The operator lost the bootstrap admin key.

1. The raw key is held only in memory (and in the sidecar file if
   auto-generated). Check the sidecar:
   ```bash
   egg-api print-paths
   cat "$(egg-api print-paths | jq -r .home_dir)/data/bootstrap_admin.key"
   ```
2. If the sidecar is gone and no env-var pin exists, the only recovery
   path is to restart with a fresh `EGG_BOOTSTRAP_ADMIN_KEY`; the new
   value is `INSERT OR IGNORE` so it creates a new row. The old admin
   row persists but is unreachable — revoke it via SQL:
   ```sql
   UPDATE api_keys SET status = 'revoked' WHERE key_id = 'admin_legacy';
   ```
3. After recovery, pin the key via `EGG_BOOTSTRAP_ADMIN_KEY` in your
   secret manager so the sidecar is no longer authoritative.

## purge

The background purge loop stopped advancing.

1. Check `/admin/v1/storage/stats` — `last_purge.last_run_at` and
   `last_purge.errors` are the source of truth.
2. If `errors > 0`, the purge raised and the loop kept going: investigate
   `event="purge_loop_tick_failed"` in the logs.
3. If `last_run_at` is stale (> 2× `purge_interval_seconds`), the loop
   task died at startup: inspect the `lifespan` traceback.
4. Manual purge (on-host):
   ```python
   from app.dependencies import container
   container.store.purge_expired_ui_sessions()
   container.store.purge_usage_events_older_than(30)
   ```
