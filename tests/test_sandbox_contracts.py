"""Contract tests for sandbox date validation, link lifecycle, probes, and fault injection."""

from __future__ import annotations

import logging
from typing import Iterator, List

import pytest
from fastapi.testclient import TestClient

from backend.app.client import get_client
from backend.app.main import create_app
from backend.app.sandbox import reset_sandbox_state


@pytest.fixture(autouse=True)
def _clean_sandbox() -> Iterator[None]:
    reset_sandbox_state()
    yield
    reset_sandbox_state()


def test_health_ready_and_lifespan_client_shutdown() -> None:
    app = create_app(retry_base=0.0, retry_factor=2.0, max_retries=3)
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert health.headers.get("X-Request-ID")

        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready", "http_client": "open", "sandbox": "active"}

        shared_http = get_client()
        assert not shared_http.is_closed

    assert shared_http.is_closed


def test_request_id_propagation_on_success_and_error() -> None:
    app = create_app(retry_base=0.0)
    with TestClient(app) as client:
        ok_resp = client.get("/health", headers={"X-Request-ID": "trace-custom-123"})
        assert ok_resp.status_code == 200
        assert ok_resp.headers["X-Request-ID"] == "trace-custom-123"

        err_resp = client.post(
            "/api/v1/accounts",
            json={"link": "missing-link"},
            headers={"X-Request-ID": "trace-err-404"},
        )
        assert err_resp.status_code == 404
        assert err_resp.headers["X-Request-ID"] == "trace-err-404"
        assert err_resp.json()["request_id"] == "trace-err-404"
        assert err_resp.json()["code"] == "link_not_found"


def test_link_lifecycle_and_cascade_cleanup() -> None:
    app = create_app(retry_base=0.0)
    with TestClient(app) as client:
        create_resp = client.post(
            "/api/v1/links",
            json={"institution": "retail_bank_sandbox", "username": "u_test"},
        )
        assert create_resp.status_code == 201
        link_id = create_resp.json()["id"]

        get_resp = client.get(f"/api/v1/links/{link_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["institution"] == "retail_bank_sandbox"

        owners_resp = client.post("/api/v1/owners", json={"link": link_id})
        assert owners_resp.status_code == 200
        assert owners_resp.json()["data"][0]["link"] == link_id

        mfa_resp = client.post("/api/v1/accounts", json={"link": link_id})
        assert mfa_resp.status_code == 428
        session_id = mfa_resp.json()["session"]

        del_resp = client.delete(f"/api/v1/links/{link_id}")
        assert del_resp.status_code == 204

        assert client.get(f"/api/v1/links/{link_id}").status_code == 404
        assert client.post("/api/v1/owners", json={"link": link_id}).status_code == 404
        assert client.delete(f"/api/v1/links/{link_id}").status_code == 404

        stale_resume = client.patch(
            "/api/v1/mfa/resume",
            json={"session": session_id, "link": link_id, "token": "123456"},
        )
        assert stale_resume.status_code == 400
        assert stale_resume.json()["code"] == "unknown_mfa_session"


@pytest.mark.parametrize(
    ("date_from", "date_to", "expected_code"),
    [
        ("2026/01/01", "2026-01-10", "invalid_date_format"),
        ("2026-1-01", "2026-01-10", "invalid_date_format"),
        ("2026-02-30", "2026-03-01", "invalid_date_value"),
        ("2026-05-10", "2026-05-01", "invalid_date_range"),
    ],
)
def test_transactions_date_validation(
    date_from: str,
    date_to: str,
    expected_code: str,
) -> None:
    app = create_app(retry_base=0.0)
    with TestClient(app) as client:
        link_id = client.post(
            "/api/v1/links",
            json={"institution": "retail_bank_sandbox"},
        ).json()["id"]

        post_resp = client.post(
            "/api/v1/transactions",
            json={"link": link_id, "date_from": date_from, "date_to": date_to},
        )
        assert post_resp.status_code == 422
        assert post_resp.json()["code"] == expected_code

        get_resp = client.get(
            "/api/v1/transactions",
            params={"link": link_id, "date_from": date_from, "date_to": date_to},
        )
        assert get_resp.status_code == 422
        assert get_resp.json()["code"] == expected_code


def test_fault_injection_end_to_end_resilience(caplog: pytest.LogCaptureFixture) -> None:
    sleeps: List[float] = []

    async def zero_sleep(delay: float) -> None:
        sleeps.append(delay)

    app = create_app(retry_base=1.0, retry_factor=2.0, max_retries=3, sleep_fn=zero_sleep)
    caplog.set_level(logging.ERROR, logger="backend.app.client")

    with TestClient(app) as client:
        link_id = client.post(
            "/api/v1/links",
            json={"institution": "retail_bank_sandbox"},
        ).json()["id"]

        # 503-once via header recovers transparently on retry
        resp_503 = client.post(
            "/api/v1/client/owners",
            json={"link": link_id},
            headers={"X-Simulate-Fault": "503-once", "X-Request-ID": "e2e-503-trace"},
        )
        assert resp_503.status_code == 200
        assert resp_503.headers["X-Request-ID"] == "e2e-503-trace"
        assert sleeps == [1.0]
        assert "e2e-503-trace" in caplog.text

        # too_many_sessions-once via header recovers transparently on retry
        sleeps.clear()
        resp_tms = client.post(
            "/api/v1/client/owners",
            json={"link": link_id},
            headers={"X-Simulate-Fault": "too_many_sessions-once", "X-Request-ID": "e2e-tms-trace"},
        )
        assert resp_tms.status_code == 200
        assert sleeps == [1.0]
        assert "e2e-tms-trace" in caplog.text

        # 423 operational_limits_reached is NOT retried and returns 423 immediately
        sleeps.clear()
        resp_423 = client.post(
            "/api/v1/client/owners",
            json={"link": link_id},
            headers={"X-Simulate-Fault": "423", "X-Request-ID": "e2e-423-trace"},
        )
        assert resp_423.status_code == 423
        assert resp_423.json()["code"] == "operational_limits_reached"
        assert resp_423.json()["public_error_code"] == "operational_limits_reached"
        assert sleeps == []
        assert "e2e-423-trace" in caplog.text


def test_fault_arming_endpoint_and_clear() -> None:
    sleeps: List[float] = []

    async def zero_sleep(delay: float) -> None:
        sleeps.append(delay)

    app = create_app(retry_base=0.5, retry_factor=2.0, max_retries=2, sleep_fn=zero_sleep)
    with TestClient(app) as client:
        link_id = client.post(
            "/api/v1/links",
            json={"institution": "retail_bank_sandbox"},
        ).json()["id"]

        arm = client.post("/api/v1/sandbox/faults", json={"fault": "503-once"})
        assert arm.status_code == 200
        assert arm.json()["armed"] == {"fault": "503-once", "remaining": 1}

        recovered = client.post("/api/v1/client/owners", json={"link": link_id})
        assert recovered.status_code == 200
        assert sleeps == [0.5]

        # Persistent 423 armed then cleared
        client.post("/api/v1/sandbox/faults", json={"fault": "423"})
        blocked = client.post("/api/v1/client/owners", json={"link": link_id})
        assert blocked.status_code == 423

        cleared = client.delete("/api/v1/sandbox/faults")
        assert cleared.status_code == 204
        ok_after = client.post("/api/v1/client/owners", json={"link": link_id})
        assert ok_after.status_code == 200
