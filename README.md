# open-finance-resilient-client

An async FastAPI + `httpx` client and local fault-injectable sandbox encoding selective retry boundaries, `request_id` correlation logging, and an HTTP `428 Precondition Required` MFA state machine.

---

## Why This Exists

Open Finance and banking aggregation APIs rarely fail cleanly with plain `200` or `500` responses:

1. An endpoint returns `HTTP 428 Precondition Required` with a multi-step MFA challenge bound strictly to one `(session, link, resource)` tuple, returning `409 Conflict` if replayed or resumed out of scope.
2. Both transient concurrency caps (`code == "too_many_sessions"`) and hard rate limits can surface as `429` or `4xx` - retrying the wrong one wastes quota, while failing to retry `too_many_sessions` drops recoverable syncs.
3. Without end-to-end `request_id` propagation across middleware, upstream error bodies, and client logs, triaging failed customer syncs slows down significantly.

Testing these error transitions directly against external sandboxes is slow and non-deterministic, so this repository pairs a lifecycle-managed async `httpx` client with an in-process Open Finance sandbox supporting controllable fault injection (`X-Simulate-Fault`).

## Scope & Intentional Non-Goals

This repository is a **standalone reference implementation and test harness** derived from real financial API integration edge cases and cleaned of vendor-specific branding and credentials.

It is **not**:

- a production banking connector or certified FAPI/Open Finance client
- backed by PostgreSQL or Redis (sandbox state lives in process memory per app instance)
- wired for real OAuth2/mTLS certificates or external credential vaults

---

## Architecture Overview

```text
Caller / Integration
   |
   v  (X-Request-ID middleware generates or propagates correlation ID)
FastAPI Entrypoint (backend/app/main.py)
   |-- /health, /ready
   |-- /api/v1/client/* --> ResilientOpenFinanceClient (backend/app/client.py)
   |                          |-- App-scoped httpx.AsyncClient (FastAPI lifespan)
   |                          |-- Exponential backoff (base * factor^attempt) + Retry-After + optional jitter
   |                          |-- Retries: 5xx + 4xx ONLY if code == "too_many_sessions"
   |                          +-- Logs request_id on every non-2xx response
   |                                 |
   |                                 v  (ASGITransport in-process or external HTTP)
   +-- /api/v1/*        --> Deterministic Sandbox (backend/app/sandbox.py)
                              |-- Links CRUD (/api/v1/links)
                              |-- Resources (/accounts, /owners, /transactions)
                              |-- HTTP 428 MFA State Machine -> PATCH /api/v1/mfa/resume
                              |-- Strict (session, link, resource) binding -> 409 Conflict
                              +-- Fault injection (X-Simulate-Fault / /api/v1/sandbox/faults)
```

---

## Core Engineering Guarantees

1. **App-Scoped Lifespan-Managed `httpx.AsyncClient`**
   FastAPI's `lifespan` initializes an isolated per-app async connection pool on startup (switching `/ready` from `503 not_ready` to `200 ready`) and closes it cleanly on shutdown without per-request socket churn or cross-app state interference.
2. **Selective Exponential Backoff, `Retry-After` & Optional Jitter (`base`, `factor`, `max_retries`, `sleep_fn`, `jitter_fn`)**
   - **Retried:** Transport errors (`httpx.RequestError`), all `5xx` responses, and `4xx` responses exclusively when the structured error payload carries `code == "too_many_sessions"`. Honors numeric `Retry-After` headers (bounded by `max_retry_after_seconds`) and optional caller-injected `jitter_fn`.
   - **Never retried:** `400`, `404`, `409`, `422`, `423`, `428`, or generic `429` (`rate_limit_exceeded`).
   - **Deterministic testing:** Injectable async `sleep_fn` allows tests to assert exact backoff schedules without waiting real wall-clock seconds.
3. **End-to-End `request_id` Observability**
   HTTP middleware extracts or generates `X-Request-ID`, attaches it to every JSON error body and response header, forwards it through the resilient proxy client, and logs `status`, `method`, `path`, `code`, and `request_id` on every non-2xx response.
4. **Strict HTTP `428` MFA State Machine**
   - `POST /api/v1/accounts` and `POST /api/v1/transactions` emit `HTTP 428 Precondition Required` with a fresh `session` UUID and `challenge_type` (`otp_token` or `app_approval`).
   - `PATCH /api/v1/mfa/resume` enforces strict binding across `(session, link, resource)` and one-time consumption, returning `409 Conflict` on link mismatch, resource mismatch, or replay.
5. **Controlled End-to-End Fault Injection**
   Send `X-Simulate-Fault` (`503-once`, `too_many_sessions-once`, `423`, `429`, `400`) or queue faults via `POST /api/v1/sandbox/faults` to inspect recovery and non-retry behavior through `/api/v1/client/*`.

---

## API Surface

| Endpoint | Method | Purpose | Key Responses |
|---|---|---|---|
| `/health` | `GET` | Liveness probe | `200` |
| `/ready` | `GET` | Readiness probe (verifies open `httpx.AsyncClient`) | `200`, `503` |
| `/api/v1/links` | `POST` | Register institution link | `201`, `422` |
| `/api/v1/links/{link_id}` | `GET` / `DELETE` | Inspect or delete link and bound state | `200`, `204`, `404` |
| `/api/v1/owners` | `POST` / `GET` | Retrieve or read account holder profile | `200`, `404`, `428` |
| `/api/v1/accounts` | `POST` / `GET` | Initiate MFA challenge (`428`) or read accounts | `200`, `404`, `428` |
| `/api/v1/transactions` | `POST` / `GET` | Validate `YYYY-MM-DD` window, trigger MFA (`428`), filter stored rows | `200`, `404`, `422`, `428` |
| `/api/v1/mfa/resume` | `PATCH` | Complete MFA challenge with strict `(session, link, resource)` binding | `200`, `400`, `409`, `422` |
| `/api/v1/sandbox/faults` | `POST` / `DELETE` | Arm or clear queued sandbox faults | `200`, `204`, `422` |
| `/api/v1/sandbox/reset` | `POST` | Reset all in-memory sandbox state | `204` |
| `/api/v1/client/*` | `POST` / `GET` / `PATCH` / `DELETE` | Proxy routes backed by `ResilientOpenFinanceClient` | Upstream status preserved |

---

## Quickstart

### Local Python Environment (Python 3.9+)

```bash
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1

pip install -r requirements-dev.txt
uvicorn backend.app.main:app --reload
```

### Run Test Suite

```bash
pytest -v
```

### Docker Compose (Non-Root Container)

```bash
docker compose up --build
```

---

## End-to-End Walkthrough

### 1. Create a Link & Trigger HTTP `428` MFA

```bash
LINK_ID=$(curl -s -X POST http://127.0.0.1:8000/api/v1/links \
  -H "Content-Type: application/json" \
  -d '{"institution": "retail_bank_sandbox", "access_mode": "recurrent"}' \
  | python -c "import sys, json; print(json.load(sys.stdin)['id'])")

curl -i -X POST http://127.0.0.1:8000/api/v1/accounts \
  -H "Content-Type: application/json" \
  -d "{\"link\": \"$LINK_ID\"}"
```

Example `428` response:

```json
{
  "code": "token_required",
  "message": "Multi-factor authentication required before retrieving accounts.",
  "request_id": "c7a89c1a-4b27-44a4-a316-5a9f57be2f10",
  "session": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "link": "link-a1b2c3d4e5f6",
  "resource": "accounts",
  "challenge_type": "otp_token"
}
```

### 2. Resume MFA (`PATCH /api/v1/mfa/resume`) & Verify Replay Rejection (`409`)

```bash
curl -s -X PATCH http://127.0.0.1:8000/api/v1/mfa/resume \
  -H "Content-Type: application/json" \
  -d "{\"session\": \"<SESSION_UUID>\", \"link\": \"$LINK_ID\", \"resource\": \"accounts\", \"token\": \"123456\"}"

curl -i -X PATCH http://127.0.0.1:8000/api/v1/mfa/resume \
  -H "Content-Type: application/json" \
  -d "{\"session\": \"<SESSION_UUID>\", \"link\": \"$LINK_ID\", \"resource\": \"accounts\", \"token\": \"123456\"}"
```

### 3. Exercise Resilient Recovery vs Non-Retry Boundary

```bash
# Transient 503 on attempt 0 is retried and succeeds (200 OK)
curl -i -X POST http://127.0.0.1:8000/api/v1/client/owners \
  -H "Content-Type: application/json" \
  -H "X-Simulate-Fault: 503-once" \
  -d "{\"link\": \"$LINK_ID\"}"

# Concurrent session cap (429 too_many_sessions) on attempt 0 is retried and succeeds (200 OK)
curl -i -X POST http://127.0.0.1:8000/api/v1/client/owners \
  -H "Content-Type: application/json" \
  -H "X-Simulate-Fault: too_many_sessions-once" \
  -d "{\"link\": \"$LINK_ID\"}"

# Operational quota lock (423 operational_limits_reached) is NOT retried
curl -i -X POST http://127.0.0.1:8000/api/v1/client/owners \
  -H "Content-Type: application/json" \
  -H "X-Simulate-Fault: 423" \
  -d "{\"link\": \"$LINK_ID\"}"
```

## Documentation

- [Error Contract, Status Matrix & Support Runbook](docs/ERROR_CONTRACT_AND_DX.md)

## License

MIT - see [LICENSE](LICENSE).
