"""FastAPI routes for the Open Finance deterministic sandbox and resilient proxy client."""

from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Depends, Header, Query, Request, Response

from backend.app.client import ResilientOpenFinanceClient, get_client
from backend.app.sandbox import (
    ConnectionCreateRequest,
    FaultArmRequest,
    MfaResumeRequest,
    ResourceFetchRequest,
    SandboxAPIError,
    TransactionsFetchRequest,
    sandbox,
)

sandbox_router = APIRouter(prefix="/api/v1", tags=["sandbox"])
client_router = APIRouter(prefix="/api/v1/client", tags=["resilient-client"])


def get_request_id(request: Request) -> str:
    """Resolve the active request correlation ID from middleware state or headers."""
    req_id = getattr(request.state, "request_id", None)
    if req_id:
        return str(req_id)
    header_id = request.headers.get("X-Request-ID")
    if header_id:
        return str(header_id)
    return str(uuid.uuid4())


def get_resilient_client(request: Request) -> ResilientOpenFinanceClient:
    """Build a ResilientOpenFinanceClient using app-scoped AsyncClient and retry policy."""
    http_client = getattr(request.app.state, "http_client", None) or get_client()
    return ResilientOpenFinanceClient(
        client=http_client,
        base=getattr(request.app.state, "retry_base", 0.5),
        factor=getattr(request.app.state, "retry_factor", 2.0),
        max_retries=getattr(request.app.state, "max_retries", 4),
        sleep_fn=getattr(request.app.state, "sleep_fn", None),
        respect_retry_after=getattr(request.app.state, "respect_retry_after", True),
        jitter_fn=getattr(request.app.state, "jitter_fn", None),
    )


@sandbox_router.post("/connections", status_code=201)
async def create_connection(
    body: ConnectionCreateRequest,
    request: Request,
    x_simulate_fault: Optional[str] = Header(default=None, alias="X-Simulate-Fault"),
) -> Dict[str, Any]:
    request_id = get_request_id(request)
    sandbox.maybe_trigger_fault(x_simulate_fault, request_id)
    connection = sandbox.create_connection(
        institution=body.institution,
        username=body.username,
        access_mode=body.access_mode,
    )
    return {**connection, "request_id": request_id}


@sandbox_router.get("/connections/{connection_id}")
async def get_connection(
    connection_id: str,
    request: Request,
    x_simulate_fault: Optional[str] = Header(default=None, alias="X-Simulate-Fault"),
) -> Dict[str, Any]:
    request_id = get_request_id(request)
    sandbox.maybe_trigger_fault(x_simulate_fault, request_id)
    connection = sandbox.require_connection(connection_id)
    return {**connection, "request_id": request_id}


@sandbox_router.delete("/connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str,
    request: Request,
    x_simulate_fault: Optional[str] = Header(default=None, alias="X-Simulate-Fault"),
) -> Response:
    request_id = get_request_id(request)
    sandbox.maybe_trigger_fault(x_simulate_fault, request_id)
    sandbox.delete_connection(connection_id)
    return Response(status_code=204)


@sandbox_router.post("/accounts")
async def fetch_accounts(
    body: ResourceFetchRequest,
    request: Request,
    x_simulate_fault: Optional[str] = Header(default=None, alias="X-Simulate-Fault"),
) -> Dict[str, Any]:
    request_id = get_request_id(request)
    sandbox.require_connection(body.connection)
    sandbox.maybe_trigger_fault(x_simulate_fault, request_id)

    require_mfa = True if body.require_mfa is None else body.require_mfa
    if require_mfa:
        challenge = sandbox.create_mfa_challenge(
            resource="accounts",
            connection=body.connection,
            challenge_type=body.challenge_type or "otp_token",
            context={"save_data": body.save_data},
        )
        raise SandboxAPIError(
            status_code=428,
            code=challenge["code"],
            message=challenge["message"],
            extra={
                "session": challenge["session"],
                "connection": challenge["connection"],
                "resource": challenge["resource"],
                "challenge_type": challenge["challenge_type"],
            },
        )

    data = sandbox.generate_accounts(body.connection, save_data=body.save_data)
    return {
        "connection": body.connection,
        "resource": "accounts",
        "request_id": request_id,
        "data": data,
    }


@sandbox_router.get("/accounts")
async def get_accounts(
    request: Request,
    connection: str = Query(...),
    x_simulate_fault: Optional[str] = Header(default=None, alias="X-Simulate-Fault"),
) -> Dict[str, Any]:
    request_id = get_request_id(request)
    sandbox.require_connection(connection)
    sandbox.maybe_trigger_fault(x_simulate_fault, request_id)
    return {
        "connection": connection,
        "resource": "accounts",
        "request_id": request_id,
        "data": sandbox.accounts.get(connection, []),
    }


@sandbox_router.post("/owners")
async def fetch_owners(
    body: ResourceFetchRequest,
    request: Request,
    x_simulate_fault: Optional[str] = Header(default=None, alias="X-Simulate-Fault"),
) -> Dict[str, Any]:
    request_id = get_request_id(request)
    sandbox.require_connection(body.connection)
    sandbox.maybe_trigger_fault(x_simulate_fault, request_id)

    require_mfa = False if body.require_mfa is None else body.require_mfa
    if require_mfa:
        challenge = sandbox.create_mfa_challenge(
            resource="owners",
            connection=body.connection,
            challenge_type=body.challenge_type or "otp_token",
            context={"save_data": body.save_data},
        )
        raise SandboxAPIError(
            status_code=428,
            code=challenge["code"],
            message=challenge["message"],
            extra={
                "session": challenge["session"],
                "connection": challenge["connection"],
                "resource": challenge["resource"],
                "challenge_type": challenge["challenge_type"],
            },
        )

    data = sandbox.generate_owners(body.connection, save_data=body.save_data)
    return {
        "connection": body.connection,
        "resource": "owners",
        "request_id": request_id,
        "data": data,
    }


@sandbox_router.get("/owners")
async def get_owners(
    request: Request,
    connection: str = Query(...),
    x_simulate_fault: Optional[str] = Header(default=None, alias="X-Simulate-Fault"),
) -> Dict[str, Any]:
    request_id = get_request_id(request)
    sandbox.require_connection(connection)
    sandbox.maybe_trigger_fault(x_simulate_fault, request_id)
    return {
        "connection": connection,
        "resource": "owners",
        "request_id": request_id,
        "data": sandbox.owners.get(connection, []),
    }


@sandbox_router.post("/transactions")
async def fetch_transactions(
    body: TransactionsFetchRequest,
    request: Request,
    x_simulate_fault: Optional[str] = Header(default=None, alias="X-Simulate-Fault"),
) -> Dict[str, Any]:
    request_id = get_request_id(request)
    sandbox.require_connection(body.connection)
    sandbox.parse_date_range(body.date_from, body.date_to)
    sandbox.maybe_trigger_fault(x_simulate_fault, request_id)

    require_mfa = True if body.require_mfa is None else body.require_mfa
    if require_mfa:
        challenge = sandbox.create_mfa_challenge(
            resource="transactions",
            connection=body.connection,
            challenge_type=body.challenge_type or "app_approval",
            context={
                "date_from": body.date_from,
                "date_to": body.date_to,
                "save_data": body.save_data,
            },
        )
        raise SandboxAPIError(
            status_code=428,
            code=challenge["code"],
            message=challenge["message"],
            extra={
                "session": challenge["session"],
                "connection": challenge["connection"],
                "resource": challenge["resource"],
                "challenge_type": challenge["challenge_type"],
            },
        )

    data = sandbox.generate_transactions(
        connection=body.connection,
        date_from=body.date_from,
        date_to=body.date_to,
        save_data=body.save_data,
    )
    return {
        "connection": body.connection,
        "resource": "transactions",
        "request_id": request_id,
        "data": data,
    }


@sandbox_router.get("/transactions")
async def get_transactions(
    request: Request,
    connection: str = Query(...),
    date_from: str = Query(...),
    date_to: str = Query(...),
    x_simulate_fault: Optional[str] = Header(default=None, alias="X-Simulate-Fault"),
) -> Dict[str, Any]:
    request_id = get_request_id(request)
    sandbox.maybe_trigger_fault(x_simulate_fault, request_id)
    rows = sandbox.filter_stored_transactions(
        connection=connection,
        date_from=date_from,
        date_to=date_to,
    )
    return {
        "connection": connection,
        "resource": "transactions",
        "request_id": request_id,
        "data": rows,
    }


@sandbox_router.patch("/mfa/resume")
async def resume_mfa(
    body: MfaResumeRequest,
    request: Request,
    x_simulate_fault: Optional[str] = Header(default=None, alias="X-Simulate-Fault"),
) -> Dict[str, Any]:
    request_id = get_request_id(request)
    sandbox.maybe_trigger_fault(x_simulate_fault, request_id)
    result = sandbox.resume_mfa(
        session=body.session,
        connection=body.connection,
        token=body.token,
        resource=body.resource,
    )
    return {**result, "request_id": request_id}


@sandbox_router.post("/sandbox/faults")
async def arm_fault(body: FaultArmRequest, request: Request) -> Dict[str, Any]:
    request_id = get_request_id(request)
    armed = sandbox.arm_fault(fault=body.fault, count=body.count)
    return {"armed": armed, "request_id": request_id}


@sandbox_router.delete("/sandbox/faults", status_code=204)
async def clear_faults() -> Response:
    sandbox.clear_faults()
    return Response(status_code=204)


@sandbox_router.post("/sandbox/reset", status_code=204)
async def reset_sandbox() -> Response:
    sandbox.reset()
    return Response(status_code=204)


async def _forward_via_resilient_client(
    request: Request,
    resilient: ResilientOpenFinanceClient,
    method: str,
    target_path: str,
    payload: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Response:
    request_id = get_request_id(request)
    headers: Dict[str, str] = {"X-Request-ID": request_id}
    fault_header = request.headers.get("X-Simulate-Fault")
    if fault_header:
        headers["X-Simulate-Fault"] = fault_header

    try:
        upstream = await resilient.request(
            method,
            target_path,
            json=payload,
            params=params,
            headers=headers,
        )
    except httpx.RequestError as exc:
        raise SandboxAPIError(
            status_code=502,
            code="upstream_transport_error",
            message=f"Upstream request failed after retries: {exc}",
        ) from exc

    if upstream.status_code == 204:
        return Response(status_code=204)

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


@client_router.post("/connections")
async def client_create_connection(
    body: ConnectionCreateRequest,
    request: Request,
    resilient: ResilientOpenFinanceClient = Depends(get_resilient_client),
) -> Response:
    return await _forward_via_resilient_client(
        request, resilient, "POST", "/api/v1/connections", payload=body.model_dump()
    )


@client_router.get("/connections/{connection_id}")
async def client_get_connection(
    connection_id: str,
    request: Request,
    resilient: ResilientOpenFinanceClient = Depends(get_resilient_client),
) -> Response:
    return await _forward_via_resilient_client(
        request, resilient, "GET", f"/api/v1/connections/{connection_id}"
    )


@client_router.delete("/connections/{connection_id}")
async def client_delete_connection(
    connection_id: str,
    request: Request,
    resilient: ResilientOpenFinanceClient = Depends(get_resilient_client),
) -> Response:
    return await _forward_via_resilient_client(
        request, resilient, "DELETE", f"/api/v1/connections/{connection_id}"
    )


@client_router.post("/accounts")
async def client_fetch_accounts(
    body: ResourceFetchRequest,
    request: Request,
    resilient: ResilientOpenFinanceClient = Depends(get_resilient_client),
) -> Response:
    return await _forward_via_resilient_client(
        request, resilient, "POST", "/api/v1/accounts", payload=body.model_dump()
    )


@client_router.post("/owners")
async def client_fetch_owners(
    body: ResourceFetchRequest,
    request: Request,
    resilient: ResilientOpenFinanceClient = Depends(get_resilient_client),
) -> Response:
    return await _forward_via_resilient_client(
        request, resilient, "POST", "/api/v1/owners", payload=body.model_dump()
    )


@client_router.post("/transactions")
async def client_fetch_transactions(
    body: TransactionsFetchRequest,
    request: Request,
    resilient: ResilientOpenFinanceClient = Depends(get_resilient_client),
) -> Response:
    return await _forward_via_resilient_client(
        request, resilient, "POST", "/api/v1/transactions", payload=body.model_dump()
    )


@client_router.get("/transactions")
async def client_get_transactions(
    request: Request,
    connection: str = Query(...),
    date_from: str = Query(...),
    date_to: str = Query(...),
    resilient: ResilientOpenFinanceClient = Depends(get_resilient_client),
) -> Response:
    return await _forward_via_resilient_client(
        request,
        resilient,
        "GET",
        "/api/v1/transactions",
        params={"connection": connection, "date_from": date_from, "date_to": date_to},
    )


@client_router.patch("/mfa/resume")
async def client_resume_mfa(
    body: MfaResumeRequest,
    request: Request,
    resilient: ResilientOpenFinanceClient = Depends(get_resilient_client),
) -> Response:
    return await _forward_via_resilient_client(
        request, resilient, "PATCH", "/api/v1/mfa/resume", payload=body.model_dump()
    )
