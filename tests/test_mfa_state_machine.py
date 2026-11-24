"""Tests for the HTTP 428 MFA multi-step state machine and strict 409 Conflict bindings."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.sandbox import reset_sandbox_state


@pytest.fixture(autouse=True)
def _clean_sandbox() -> Iterator[None]:
    reset_sandbox_state()
    yield
    reset_sandbox_state()


@pytest.fixture
def api_client() -> Iterator[TestClient]:
    app = create_app(retry_base=0.0, retry_factor=2.0, max_retries=3)
    with TestClient(app) as client:
        yield client


def _create_link(client: TestClient, institution: str = "retail_bank_sandbox") -> str:
    resp = client.post(
        "/api/v1/links",
        json={"institution": institution, "access_mode": "recurrent"},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def test_accounts_mfa_428_to_resume_and_get(api_client: TestClient) -> None:
    link_id = _create_link(api_client)

    challenge_resp = api_client.post("/api/v1/accounts", json={"link": link_id})
    assert challenge_resp.status_code == 428
    challenge = challenge_resp.json()
    assert challenge["code"] == "token_required"
    assert challenge["link"] == link_id
    assert challenge["resource"] == "accounts"
    assert challenge["challenge_type"] == "otp_token"
    assert challenge["session"]
    assert challenge["request_id"]

    resume_resp = api_client.patch(
        "/api/v1/mfa/resume",
        json={
            "session": challenge["session"],
            "link": link_id,
            "resource": "accounts",
            "token": "654321",
        },
    )
    assert resume_resp.status_code == 200
    body = resume_resp.json()
    assert body["status"] == "completed"
    assert body["resource"] == "accounts"
    assert len(body["data"]) == 2
    assert body["data"][0]["link"] == link_id

    stored = api_client.get("/api/v1/accounts", params={"link": link_id})
    assert stored.status_code == 200
    assert len(stored.json()["data"]) == 2


def test_transactions_mfa_428_resume_and_date_filtering(api_client: TestClient) -> None:
    link_id = _create_link(api_client)
    today = date.today()
    date_from = str(today - timedelta(days=2))
    date_to = str(today)

    challenge_resp = api_client.post(
        "/api/v1/transactions",
        json={"link": link_id, "date_from": date_from, "date_to": date_to},
    )
    assert challenge_resp.status_code == 428
    challenge = challenge_resp.json()
    assert challenge["resource"] == "transactions"
    assert challenge["challenge_type"] == "app_approval"

    resume_resp = api_client.patch(
        "/api/v1/mfa/resume",
        json={
            "session": challenge["session"],
            "link": link_id,
            "resource": "transactions",
            "token": "approved",
        },
    )
    assert resume_resp.status_code == 200
    tx_rows = resume_resp.json()["data"]
    assert len(tx_rows) == 3
    assert all(date_from <= row["value_date"] <= date_to for row in tx_rows)

    narrow_from = str(today - timedelta(days=1))
    stored = api_client.get(
        "/api/v1/transactions",
        params={"link": link_id, "date_from": narrow_from, "date_to": date_to},
    )
    assert stored.status_code == 200
    assert len(stored.json()["data"]) == 2


def test_mfa_409_on_link_mismatch_resource_mismatch_and_replay(api_client: TestClient) -> None:
    link_a = _create_link(api_client, "bank_a")
    link_b = _create_link(api_client, "bank_b")

    challenge = api_client.post("/api/v1/accounts", json={"link": link_a}).json()
    session_id = challenge["session"]

    wrong_link = api_client.patch(
        "/api/v1/mfa/resume",
        json={
            "session": session_id,
            "link": link_b,
            "resource": "accounts",
            "token": "123456",
        },
    )
    assert wrong_link.status_code == 409
    assert wrong_link.json()["code"] == "session_link_mismatch"

    wrong_resource = api_client.patch(
        "/api/v1/mfa/resume",
        json={
            "session": session_id,
            "link": link_a,
            "resource": "transactions",
            "token": "123456",
        },
    )
    assert wrong_resource.status_code == 409
    assert wrong_resource.json()["code"] == "session_resource_mismatch"

    valid_resume = api_client.patch(
        "/api/v1/mfa/resume",
        json={
            "session": session_id,
            "link": link_a,
            "resource": "accounts",
            "token": "123456",
        },
    )
    assert valid_resume.status_code == 200

    replay_resp = api_client.patch(
        "/api/v1/mfa/resume",
        json={
            "session": session_id,
            "link": link_a,
            "resource": "accounts",
            "token": "123456",
        },
    )
    assert replay_resp.status_code == 409
    assert replay_resp.json()["code"] == "session_already_used"


def test_mfa_unknown_session_and_blank_token_validation(api_client: TestClient) -> None:
    link_id = _create_link(api_client)

    unknown = api_client.patch(
        "/api/v1/mfa/resume",
        json={"session": "non-existent", "link": link_id, "token": "123456"},
    )
    assert unknown.status_code == 400
    assert unknown.json()["code"] == "unknown_mfa_session"

    challenge = api_client.post("/api/v1/accounts", json={"link": link_id}).json()
    blank = api_client.patch(
        "/api/v1/mfa/resume",
        json={"session": challenge["session"], "link": link_id, "token": "   "},
    )
    assert blank.status_code == 422
    assert blank.json()["code"] == "invalid_mfa_token"


def test_mfa_end_to_end_via_resilient_client_endpoints(api_client: TestClient) -> None:
    link_resp = api_client.post(
        "/api/v1/client/links",
        json={"institution": "retail_bank_sandbox"},
    )
    assert link_resp.status_code == 201
    link_id = link_resp.json()["id"]

    challenge_resp = api_client.post(
        "/api/v1/client/accounts",
        json={"link": link_id},
        headers={"X-Request-ID": "client-mfa-trace-1"},
    )
    assert challenge_resp.status_code == 428
    assert challenge_resp.headers["X-Request-ID"] == "client-mfa-trace-1"
    session_id = challenge_resp.json()["session"]

    resume_resp = api_client.patch(
        "/api/v1/client/mfa/resume",
        json={"session": session_id, "link": link_id, "resource": "accounts", "token": "999999"},
    )
    assert resume_resp.status_code == 200
    assert resume_resp.json()["status"] == "completed"

    replay_resp = api_client.patch(
        "/api/v1/client/mfa/resume",
        json={"session": session_id, "link": link_id, "resource": "accounts", "token": "999999"},
    )
    assert replay_resp.status_code == 409
    assert replay_resp.json()["code"] == "session_already_used"


def test_mfa_resume_allows_omitted_resource_and_resolves_from_session(api_client: TestClient) -> None:
    link_id = _create_link(api_client)
    challenge = api_client.post("/api/v1/accounts", json={"link": link_id}).json()

    resume_resp = api_client.patch(
        "/api/v1/mfa/resume",
        json={"session": challenge["session"], "link": link_id, "token": "654321"},
    )
    assert resume_resp.status_code == 200
    assert resume_resp.json()["resource"] == "accounts"
    assert resume_resp.json()["status"] == "completed"
