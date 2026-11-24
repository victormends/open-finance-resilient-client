"""In-process FastAPI ASGITransport fault-injection benchmark for open-finance-resilient-client."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.app.main import create_app
from backend.app.sandbox import reset_sandbox_state


async def run_benchmark() -> Dict[str, Any]:
    reset_sandbox_state()
    sleeps: List[float] = []

    async def recorded_sleep(delay: float) -> None:
        sleeps.append(delay)

    app = create_app(
        retry_base=0.5,
        retry_factor=2.0,
        max_retries=3,
        sleep_fn=recorded_sleep,
    )

    scenarios: List[Dict[str, Any]] = []
    started_all = time.perf_counter()

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://sandbox.local",
        ) as client:
            conn_resp = await client.post(
                "/api/v1/client/connections",
                json={"institution": "retail_bank_sandbox", "access_mode": "recurrent"},
                headers={"X-Request-ID": "bench-init-conn"},
            )
            assert conn_resp.status_code == 201
            conn_a = conn_resp.json()["id"]

            conn_b_resp = await client.post(
                "/api/v1/client/connections",
                json={"institution": "retail_bank_sandbox_b", "access_mode": "single"},
                headers={"X-Request-ID": "bench-init-conn-b"},
            )
            assert conn_b_resp.status_code == 201
            conn_b = conn_b_resp.json()["id"]

            # 1. 503-once transient recovery
            sleeps.clear()
            t0 = time.perf_counter()
            r503 = await client.post(
                "/api/v1/client/owners",
                json={"connection": conn_a},
                headers={"X-Simulate-Fault": "503-once", "X-Request-ID": "bench-503"},
            )
            ok_503 = (
                r503.status_code == 200
                and sleeps == [0.5]
                and r503.headers.get("X-Request-ID") == "bench-503"
                and r503.json().get("request_id") == "bench-503"
            )
            scenarios.append(
                {
                    "scenario": "503_retry_recovery",
                    "passed": ok_503,
                    "status": r503.status_code,
                    "retries": len(sleeps),
                    "request_id_propagated": r503.headers.get("X-Request-ID") == "bench-503",
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 3),
                }
            )

            # 2. concurrent_session_limit-once 429 retry recovery
            sleeps.clear()
            t0 = time.perf_counter()
            rcsl = await client.post(
                "/api/v1/client/owners",
                json={"connection": conn_a},
                headers={
                    "X-Simulate-Fault": "concurrent_session_limit-once",
                    "X-Request-ID": "bench-csl",
                },
            )
            ok_csl = (
                rcsl.status_code == 200
                and sleeps == [0.5]
                and rcsl.headers.get("X-Request-ID") == "bench-csl"
                and rcsl.json().get("request_id") == "bench-csl"
            )
            scenarios.append(
                {
                    "scenario": "concurrent_session_limit_retry",
                    "passed": ok_csl,
                    "status": rcsl.status_code,
                    "retries": len(sleeps),
                    "request_id_propagated": rcsl.headers.get("X-Request-ID") == "bench-csl",
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 3),
                }
            )

            # 3. Non-retry boundary (429 rate_limit_exceeded, 423 operational_limits_reached, 428 mfa_challenge_required)
            sleeps.clear()
            t0 = time.perf_counter()
            r429 = await client.post(
                "/api/v1/client/owners",
                json={"connection": conn_a},
                headers={"X-Simulate-Fault": "429", "X-Request-ID": "bench-429"},
            )
            r423 = await client.post(
                "/api/v1/client/owners",
                json={"connection": conn_a},
                headers={"X-Simulate-Fault": "423", "X-Request-ID": "bench-423"},
            )
            r428 = await client.post(
                "/api/v1/client/accounts",
                json={"connection": conn_a},
                headers={"X-Request-ID": "bench-428"},
            )
            ok_non_retry = (
                sleeps == []
                and r429.status_code == 429
                and r429.json().get("code") == "rate_limit_exceeded"
                and r429.json().get("request_id") == "bench-429"
                and r423.status_code == 423
                and r423.json().get("code") == "operational_limits_reached"
                and r423.json().get("request_id") == "bench-423"
                and r428.status_code == 428
                and r428.json().get("code") == "mfa_challenge_required"
                and r428.json().get("request_id") == "bench-428"
            )
            scenarios.append(
                {
                    "scenario": "non_retry_429_423_428_boundary",
                    "passed": ok_non_retry,
                    "statuses": [r429.status_code, r423.status_code, r428.status_code],
                    "retries": len(sleeps),
                    "request_id_propagated": True,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 3),
                }
            )

            # 4. MFA (session, connection, resource) mismatch & replay rejection (409)
            sleeps.clear()
            t0 = time.perf_counter()
            session_id = r428.json()["session"]
            r_conn_mismatch = await client.patch(
                "/api/v1/client/mfa/resume",
                json={
                    "session": session_id,
                    "connection": conn_b,
                    "resource": "accounts",
                    "token": "123456",
                },
                headers={"X-Request-ID": "bench-mfa-conn-mismatch"},
            )
            r_res_mismatch = await client.patch(
                "/api/v1/client/mfa/resume",
                json={
                    "session": session_id,
                    "connection": conn_a,
                    "resource": "transactions",
                    "token": "123456",
                },
                headers={"X-Request-ID": "bench-mfa-res-mismatch"},
            )
            r_valid = await client.patch(
                "/api/v1/client/mfa/resume",
                json={
                    "session": session_id,
                    "connection": conn_a,
                    "resource": "accounts",
                    "token": "123456",
                },
                headers={"X-Request-ID": "bench-mfa-valid"},
            )
            r_replay = await client.patch(
                "/api/v1/client/mfa/resume",
                json={
                    "session": session_id,
                    "connection": conn_a,
                    "resource": "accounts",
                    "token": "123456",
                },
                headers={"X-Request-ID": "bench-mfa-replay"},
            )
            ok_mfa = (
                sleeps == []
                and r_conn_mismatch.status_code == 409
                and r_conn_mismatch.json().get("code") == "session_connection_mismatch"
                and r_conn_mismatch.json().get("request_id") == "bench-mfa-conn-mismatch"
                and r_res_mismatch.status_code == 409
                and r_res_mismatch.json().get("code") == "session_resource_mismatch"
                and r_res_mismatch.json().get("request_id") == "bench-mfa-res-mismatch"
                and r_valid.status_code == 200
                and r_valid.json().get("status") == "completed"
                and r_valid.json().get("request_id") == "bench-mfa-valid"
                and r_replay.status_code == 409
                and r_replay.json().get("code") == "session_already_used"
                and r_replay.json().get("request_id") == "bench-mfa-replay"
            )
            scenarios.append(
                {
                    "scenario": "mfa_binding_mismatch_and_409_replay",
                    "passed": ok_mfa,
                    "statuses": [
                        r_conn_mismatch.status_code,
                        r_res_mismatch.status_code,
                        r_valid.status_code,
                        r_replay.status_code,
                    ],
                    "retries": len(sleeps),
                    "request_id_propagated": True,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 3),
                }
            )

    total_elapsed_ms = round((time.perf_counter() - started_all) * 1000.0, 3)
    passed_count = sum(1 for item in scenarios if item["passed"])
    return {
        "harness": "in-process-asgi-fault-injection",
        "total_scenarios": len(scenarios),
        "passed_scenarios": passed_count,
        "all_passed": passed_count == len(scenarios),
        "total_elapsed_ms": total_elapsed_ms,
        "scenarios": scenarios,
    }


def main() -> int:
    report = asyncio.run(run_benchmark())
    print("=== Open Finance Resilient Client ASGITransport Harness ===")
    print(json.dumps(report, indent=2))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
