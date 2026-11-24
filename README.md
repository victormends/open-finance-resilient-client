# Open Finance Resilient Client

Reference implementation of an **async Open Finance HTTP client**, **lifespan-managed FastAPI service**, **fault-injectable in-process sandbox**, and **HTTP `428 Precondition Required` MFA state machine**.

Built to demonstrate production-grade resilience patterns for financial aggregation APIs: selective backoff on transient capacity errors, strict non-retry boundaries on quota/client errors, `request_id` correlation across every non-2xx path, and deterministic multi-step institution authentication recovery.

---

## Architecture Overview

```text
Caller / Integration
   │
   ▼  (X-Request-ID middleware generates or propagates correlation ID)
FastAPI Entrypoint (backend/app/main.py)
   ├── /health, /ready
   ├── /api/v1/client/* ──▶ ResilientOpenFinanceClient (backend/app/client.py)
   │                             ├── Shared httpx.AsyncClient (FastAPI lifespan)
   │                             ├── Exponential backoff (base * factor^attempt)
   │                             ├── Retries: 5xx + 4xx ONLY if code == "too_many_sessions"
   │                             └── Logs request_id on every non-2xx response
   │                                    │
   │                                    ▼  (ASGITransport in-process or external HTTP)
   └── /api/v1/*        ──▶ Deterministic Sandbox (backend/app/sandbox.py)
                                 ├── Links CRUD (/api/v1/links)
                                 ├── Resources (/accounts, /owners, /transactions)
                                 ├── HTTP 428 MFA State Machine -> PATCH /api/v1/mfa/resume
                                 ├── Strict (session, link, resource) binding -> 409 Conflict
                                 └── Fault injection (X-Simulate-Fault / /api/v1/sandbox/faults)
```

---

## Core Engineering Guarantees

1. **Lifespan-Managed Shared `httpx.AsyncClient`**
   `get_client()` and `close_client()` manage a single pooled async HTTP client initialized on FastAPI startup and gracefully closed on shutdown. No per-request socket churn or leaked connection pools.
2. **Selective Exponential Backoff (`base`, `factor`, `max_retries`, `sleep_fn`)**
   - **Retried:** Transport errors (`httpx.RequestError`), all `5xx` responses, and `4xx` responses **exclusively** when the structured error payload carries `code == "too_many_sessions"`.
   - **Never retried:** `400`, `404`, `409`, `422`, `423`, `428`, or generic `429` (`rate_limit_exceeded`).
   - **Deterministic testing:** Injectable async `sleep_fn` allows tests to assert exact backoff schedules (`base * factor^attempt`) in zero wall-clock time.
3. **End-to-End `request_id` Observability**
   HTTP middleware extracts or generates `X-Request-ID`, attaches it to every JSON error body and response header, forwards it through the resilient proxy client, and logs `status`, `method`, `path`, `code`, and `request_id` on every non-2xx upstream response.
4. **Strict HTTP `428` MFA State Machine**
   - `POST /api/v1/accounts` and `POST /api/v1/transactions` emit `HTTP 428 Precondition Required` with a fresh `session` UUID and `challenge_type` (`otp_token` or `app_approval`).
   - `PATCH /api/v1/mfa/resume` enforces strict binding across `(session, link, resource)` and one-time consumption, returning `409 Conflict` on link mismatch, resource mismatch, or replay.
5. **Controlled End-to-End Fault Injection**
   Send `X-Simulate-Fault` (`503-once`, `too_many_sessions-once`, `423`, `429`, `400`) or queue faults via `POST /api/v1/sandbox/faults` to observe real retry recovery and non-retry boundaries through `/api/v1/client/*`.

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

pip install -r requirements.txt
uvicorn backend.app.main:app --reload
```

### Run Test Suite

```bash
pytest -v
```

### Docker Compose

```bash
docker compose up --build
```

---

## End-to-End Walkthrough

### 1. Create a Link & Trigger HTTP `428` MFA

```bash
# Create a sandbox link
LINK_ID=$(curl -s -X POST http://127.0.0.1:8000/api/v1/links \
  -H "Content-Type: application/json" \
  -d '{"institution": "retail_bank_sandbox", "access_mode": "recurrent"}' \
  | python -c "import sys, json; print(json.load(sys.stdin)['id'])")

# Initiate accounts sync -> returns HTTP 428 Precondition Required
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

### 2. Resume MFA (`PATCH /api/v1/mfa/resume`) & Observe Replay Protection (`409`)

```bash
# Resume challenge with valid session/link/resource -> returns 200 OK + account payload
curl -s -X PATCH http://127.0.0.1:8000/api/v1/mfa/resume \
  -H "Content-Type: application/json" \
  -d "{\"session\": \"<SESSION_UUID>\", \"link\": \"$LINK_ID\", \"resource\": \"accounts\", \"token\": \"123456\"}"

# Replaying the same session immediately returns 409 Conflict (session_already_used)
curl -i -X PATCH http://127.0.0.1:8000/api/v1/mfa/resume \
  -H "Content-Type: application/json" \
  -d "{\"session\": \"<SESSION_UUID>\", \"link\": \"$LINK_ID\", \"resource\": \"accounts\", \"token\": \"123456\"}"
```

### 3. Demonstrate Resilient Recovery vs Non-Retry Boundary

```bash
# Transient 503 on attempt 0 is automatically retried and succeeds (200 OK)
curl -i -X POST http://127.0.0.1:8000/api/v1/client/owners \
  -H "Content-Type: application/json" \
  -H "X-Simulate-Fault: 503-once" \
  -d "{\"link\": \"$LINK_ID\"}"

# Concurrent session cap (429 too_many_sessions) on attempt 0 is retried and succeeds (200 OK)
curl -i -X POST http://127.0.0.1:8000/api/v1/client/owners \
  -H "Content-Type: application/json" \
  -H "X-Simulate-Fault: too_many_sessions-once" \
  -d "{\"link\": \"$LINK_ID\"}"

# Operational quota lock (423 operational_limits_reached) is NOT retried and returns 423 immediately
curl -i -X POST http://127.0.0.1:8000/api/v1/client/owners \
  -H "Content-Type: application/json" \
  -H "X-Simulate-Fault: 423" \
  -d "{\"link\": \"$LINK_ID\"}"
```

---

## Documentation

- [Error Contract, Status Matrix & Support Runbook](docs/ERROR_CONTRACT_AND_DX.md)

## License

[MIT](LICENSE)
