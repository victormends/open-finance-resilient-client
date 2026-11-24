"""Deterministic unit tests for ResilientOpenFinanceClient retry and request_id logging."""

from __future__ import annotations

import logging
from typing import List

import httpx
import pytest

from backend.app.client import (
    ResilientOpenFinanceClient,
    api_post,
    close_client,
    get_client,
    init_client,
    request_with_retry,
    set_client,
)


def _make_recorder_sleeper(recorded: List[float]):
    async def _sleep(delay: float) -> None:
        recorded.append(delay)

    return _sleep


@pytest.mark.asyncio
async def test_5xx_exponential_backoff_and_recovery(caplog: pytest.LogCaptureFixture) -> None:
    attempts = 0
    sleeps: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            return httpx.Response(
                503,
                json={"code": "upstream_unavailable", "request_id": f"req-5xx-{attempts}"},
            )
        return httpx.Response(200, json={"ok": True})

    caplog.set_level(logging.WARNING, logger="backend.app.client")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://sandbox.example",
    ) as http_client:
        client = ResilientOpenFinanceClient(
            client=http_client,
            base=3.0,
            factor=2.0,
            max_retries=4,
            sleep_fn=_make_recorder_sleeper(sleeps),
        )
        response = await client.post("/api/v1/owners", json={"connection": "conn-1"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert attempts == 3
    assert sleeps == [3.0, 6.0]
    assert "req-5xx-1" in caplog.text
    assert "req-5xx-2" in caplog.text


@pytest.mark.asyncio
async def test_5xx_exhausts_max_retries_and_returns_last_response() -> None:
    attempts = 0
    sleeps: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            502,
            json={"code": "bad_gateway", "request_id": f"req-502-{attempts}"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://sandbox.example",
    ) as http_client:
        client = ResilientOpenFinanceClient(
            client=http_client,
            base=2.0,
            factor=2.0,
            max_retries=3,
            sleep_fn=_make_recorder_sleeper(sleeps),
        )
        response = await client.get("/api/v1/accounts")

    assert response.status_code == 502
    assert attempts == 4
    assert sleeps == [2.0, 4.0, 8.0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (400, {"code": "invalid_request", "request_id": "req-400"}),
        (404, {"code": "connection_not_found", "request_id": "req-404"}),
        (409, {"code": "session_already_used", "request_id": "req-409"}),
        (422, {"code": "invalid_date_range", "request_id": "req-422"}),
        (
            423,
            {
                "code": "operational_limits_reached",
                "public_error_code": "operational_limits_reached",
                "request_id": "req-423",
            },
        ),
        (
            428,
            {
                "code": "mfa_challenge_required",
                "session": "sess-1",
                "challenge_type": "otp_token",
                "request_id": "req-428",
            },
        ),
        (429, {"code": "rate_limit_exceeded", "request_id": "req-429-generic"}),
    ],
)
async def test_4xx_non_retryable_never_retries_and_logs_request_id(
    status_code: int,
    payload: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0
    sleeps: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code, json=payload)

    caplog.set_level(logging.ERROR, logger="backend.app.client")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://sandbox.example",
    ) as http_client:
        client = ResilientOpenFinanceClient(
            client=http_client,
            base=1.5,
            factor=2.0,
            max_retries=5,
            sleep_fn=_make_recorder_sleeper(sleeps),
        )
        response = await client.post("/api/v1/transactions", json={"connection": "conn-1"})

    assert response.status_code == status_code
    assert attempts == 1
    assert sleeps == []
    assert payload["request_id"] in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body_factory",
    [
        lambda n: {"code": "concurrent_session_limit", "request_id": f"req-csl-{n}"},
        lambda n: [{"code": "concurrent_session_limit", "request_id": f"req-csl-{n}"}],
        lambda n: {"detail": {"code": "concurrent_session_limit", "request_id": f"req-csl-{n}"}},
    ],
)
async def test_4xx_concurrent_session_limit_retries_and_recovers(
    body_factory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0
    sleeps: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json=body_factory(attempts))
        return httpx.Response(200, json={"recovered": True})

    caplog.set_level(logging.ERROR, logger="backend.app.client")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://sandbox.example",
    ) as http_client:
        client = ResilientOpenFinanceClient(
            client=http_client,
            base=1.0,
            factor=3.0,
            max_retries=2,
            sleep_fn=_make_recorder_sleeper(sleeps),
        )
        response = await client.post("/api/v1/accounts", json={"connection": "conn-1"})

    assert response.status_code == 200
    assert response.json() == {"recovered": True}
    assert attempts == 2
    assert sleeps == [1.0]
    assert "req-csl-1" in caplog.text


@pytest.mark.asyncio
async def test_non_2xx_3xx_and_header_fallback_request_id_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"X-Request-ID": "hdr-req-302"},
            text="Redirect",
        )

    caplog.set_level(logging.WARNING, logger="backend.app.client")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://sandbox.example",
    ) as http_client:
        client = ResilientOpenFinanceClient(
            client=http_client,
            base=1.0,
            factor=2.0,
            max_retries=2,
            sleep_fn=_make_recorder_sleeper([]),
        )
        response = await client.get("/api/v1/connections/conn-1")

    assert response.status_code == 302
    assert "hdr-req-302" in caplog.text


@pytest.mark.asyncio
async def test_transport_error_retries_and_raises_when_exhausted() -> None:
    attempts = 0
    sleeps: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("connection reset", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://sandbox.example",
    ) as http_client:
        client = ResilientOpenFinanceClient(
            client=http_client,
            base=0.5,
            factor=2.0,
            max_retries=2,
            sleep_fn=_make_recorder_sleeper(sleeps),
        )
        with pytest.raises(httpx.ConnectError):
            await client.get("/api/v1/owners")

    assert attempts == 3
    assert sleeps == [0.5, 1.0]


@pytest.mark.asyncio
async def test_shared_client_lifecycle_get_and_close() -> None:
    set_client(None)
    client = init_client(base_url="https://sandbox.example")
    assert get_client() is client
    assert not client.is_closed

    await close_client()
    assert client.is_closed


@pytest.mark.asyncio
async def test_module_level_wrappers_use_shared_client_with_sleeper() -> None:
    attempts = 0
    sleeps: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"code": "upstream_unavailable", "request_id": "req-wrap-1"})
        return httpx.Response(201, json={"id": "conn-wrap"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://sandbox.example",
    ) as http_client:
        set_client(http_client)
        try:
            resp = await api_post(
                "/api/v1/connections",
                payload={"institution": "retail_bank_sandbox"},
                base=2.0,
                factor=2.0,
                max_retries=2,
                sleep_fn=_make_recorder_sleeper(sleeps),
            )
            direct = await request_with_retry(
                "POST",
                "/api/v1/connections",
                json={"institution": "retail_bank_sandbox"},
                base=2.0,
                factor=2.0,
                max_retries=1,
                sleep_fn=_make_recorder_sleeper(sleeps),
            )
        finally:
            set_client(None)

    assert resp.status_code == 201
    assert direct.status_code == 201
    assert attempts == 3
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_retry_after_header_and_jitter_fn_applied_deterministically() -> None:
    attempts = 0
    sleeps: List[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "5"},
                json={"code": "concurrent_session_limit", "request_id": "req-ra-1"},
            )
        if attempts == 2:
            return httpx.Response(
                503,
                headers={"Retry-After": "120"},  # capped at max_retry_after_seconds=10
                json={"code": "upstream_busy", "request_id": "req-ra-2"},
            )
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://sandbox.example",
    ) as http_client:
        client = ResilientOpenFinanceClient(
            client=http_client,
            base=1.0,
            factor=2.0,
            max_retries=3,
            sleep_fn=_make_recorder_sleeper(sleeps),
            respect_retry_after=True,
            max_retry_after_seconds=10.0,
            jitter_fn=lambda d: d + 0.25,
        )
        response = await client.post("/api/v1/accounts", json={"connection": "conn-1"})

    assert response.status_code == 200
    assert attempts == 3
    assert sleeps == [5.25, 10.25]
