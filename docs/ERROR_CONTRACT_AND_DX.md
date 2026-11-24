# Error Contract, DX Analysis & Support Runbook

This document defines the HTTP error contract enforced by `ResilientOpenFinanceClient` and the Open Finance sandbox, explains common status-code ambiguities in financial aggregation APIs, and provides an operational runbook for support and on-call engineers.

---

## 1. Error Classification Matrix (`423` vs `429` vs `400` vs `409` vs `428`)

Switching solely on HTTP status codes breaks down when upstream providers overload status codes or diverge from documentation. The client classifies responses using **both HTTP status and machine-readable `code` fields**.

| HTTP Status | Error `code` | Semantics | Retryable? | Required Caller / Client Action |
|---|---|---|---|---|
| `400` | `invalid_request`, `unknown_mfa_session` | Malformed input or expired/non-existent MFA session reference | **No** | Fix caller payload or start a new resource sync (`POST`) to mint a fresh session. |
| `404` | `link_not_found` | Link was never created or was deleted | **No** | Prompt user to re-create consent via `POST /api/v1/links`. |
| `409` | `session_link_mismatch`, `session_resource_mismatch`, `session_already_used` | Strict state-machine invariant violation or replay attempt | **No** | Do not retry with old `session`. Re-initiate resource `POST` if fresh data is needed. |
| `422` | `validation_error`, `invalid_date_format`, `invalid_date_range`, `invalid_mfa_token` | Schema error, non-`YYYY-MM-DD` date, `date_from > date_to`, or blank MFA token | **No** | Correct request parameters before re-issuing. |
| `423` | `operational_limits_reached` (`public_error_code`) | Regulatory or institution operational quota exhausted | **No** | Do **not** blindly retry. Surface quota state, defer next sync until window reset, and track `request_id`. |
| `428` | `token_required` | Institution requires interactive multi-factor authentication | **No** | Pause sync, collect MFA factor (`challenge_type`), and call `PATCH /api/v1/mfa/resume`. |
| `429` | `too_many_sessions` | Temporary concurrency ceiling on upstream institution sessions | **Yes** | Automatically retried with exponential backoff (`base * factor^attempt`). |
| `429` | `rate_limit_exceeded` | Sustained API throughput limit without session-slot semantics | **No** (strict policy) | Respect downstream rate budgets / queue externally rather than tight-looping. |
| `5xx` | `upstream_unavailable`, `500`/`502`/`503`/`504` | Transient gateway or institution connector failure | **Yes** | Automatically retried with exponential backoff; escalates after `max_retries`. |

---

## 2. Why `423` vs `429` vs `400` Causes Silent Production Failures

In real-world Open Finance integrations, operational quota exhaustion frequently suffers from a **three-way contract drift**:

1. **Documentation claims `400 Bad Request`** (treating quota exhaustion as an invalid request parameter).
2. **Error reference tables list `429 Too Many Requests`** (treating it as standard rate limiting).
3. **Runtime connectors return `423 Locked`** (repurposing WebDAV's resource-lock status code to signal a temporary regulatory retrieval lock).

### Failure Mode
If an integration switches strictly on `status_code == 429` to trigger backoff or `status_code == 400` to raise an alert, `423 Locked` falls into an unhandled fallback branch — producing silent data gaps or infinite naive retries against a hard quota ceiling.

### Contract Rule Implemented Here
- **Detect operational quotas by structured `code` / `public_error_code` (`operational_limits_reached`)**, not by status code guesswork.
- **Keep `4xx` non-retryable by default**, carving out a **single explicit allowlist rule** (`code == "too_many_sessions"`) for transient concurrency slots.
- **Log `request_id` on every non-2xx response** regardless of whether the request will be retried or surfaced to the caller.

---

## 3. HTTP `428 Precondition Required` MFA State Machine

```text
POST /api/v1/accounts  (or /api/v1/transactions)
   │
   ├── Invalid link       ──▶ 404 link_not_found
   ├── Invalid date range ──▶ 422 invalid_date_range
   └── Valid link         ──▶ 428 Precondition Required
                                { code: "token_required", session, link, resource, challenge_type }
                                  │
                                  ▼
                      PATCH /api/v1/mfa/resume
                                  │
        ┌─────────────────────────┼─────────────────────────┐
        ▼                         ▼                         ▼
   Unknown session          Link/resource mismatch     Already consumed
   400 unknown_mfa_session  or replay                  (used == true)
                            409 Conflict               409 Conflict
                                  │
                                  ▼ (All bindings valid + non-empty token)
                             200 OK (session marked used, data materialized)
```

### Binding Invariants
1. **Link binding:** `resume.link` must match `session.link` (`409 session_link_mismatch`).
2. **Resource binding:** When `resume.resource` is supplied, it must match `session.resource` (`409 session_resource_mismatch`).
3. **Single-use consumption:** Once consumed, `session.used = True`; any subsequent replay returns `409 session_already_used`.
4. **Cascade on link deletion:** `DELETE /api/v1/links/{link_id}` clears all cached data and invalidates all pending MFA sessions for that link.

---

## 4. Developer Experience (DX) Recommendations for Open Finance Providers

1. **Standardize operational quota responses on `429` with explicit `Retry-After`:** Eliminate `400`/`423`/`429` ambiguity and include machine-readable reset timestamps.
2. **Expose machine-readable token/session expiry (`expires_at`):** Always return ISO-8601 expiry timestamps in auth and `428` challenge payloads rather than documenting TTLs solely in prose.
3. **Return `request_id` in both `X-Request-ID` header and JSON error envelope:** Ensures proxies, load balancers, and application logs correlate identically even on gateway timeouts or empty bodies.
4. **Provide deterministic sandbox fault-simulation headers:** Built-in fault injection (`503-once`, `too_many_sessions-once`, `423`) lets integrators verify backoff and circuit breakers in CI without waiting for live outages.

---

## 5. Support & On-Call Triage Runbook

### Step 1: Correlate by `request_id`
Every non-2xx client call logs:
```text
ERROR backend.app.client - Open Finance non-2xx response: status=423 method=POST path=/api/v1/owners request_id=<uuid> code=operational_limits_reached
```
Search centralized logs by `request_id` (and verify the matching `X-Request-ID` HTTP response header) to reconstruct the full retry timeline:
- **Multiple attempts sharing the same `request_id` ending in `200`:** Transient `5xx` or `too_many_sessions` self-healed via exponential backoff. No incident action unless error rate breaches SLO.
- **Multiple attempts ending in `503` after `max_retries`:** Sustained upstream connector outage. Escalate with the final `request_id` and timestamp.
- **Single attempt ending in `423`:** Operational quota reached (`operational_limits_reached`). Do **not** increase retry count; verify institution quota schedule.

### Step 2: Recovering Stuck or Failed MFA (`428` / `409` / `400`)
1. **User abandoned or token expired (`400 unknown_mfa_session`):** Do not retry `PATCH /api/v1/mfa/resume`. Trigger a fresh `POST /api/v1/{resource}` to issue a new `session` UUID.
2. **Client sent wrong resource or link (`409 session_link_mismatch` / `409 session_resource_mismatch`):** Inspect client-side session cache keys; sessions must be indexed by `(link, resource)`, never globally per user.
3. **Duplicate callback / double-submit (`409 session_already_used`):** Treat as idempotent completion if the first response was persisted; otherwise issue a new `POST /api/v1/{resource}` sync.
4. **Deleted or revoked consent (`404 link_not_found`):** Re-onboard the user via `POST /api/v1/links` before retrying resource retrieval.
