"""Deterministic in-process Open Finance sandbox and HTTP 428 MFA state machine."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SUPPORTED_FAULTS: Set[str] = {
    "503",
    "503-once",
    "concurrent_session_limit",
    "concurrent_session_limit-once",
    "423",
    "423-once",
    "429",
    "429-once",
    "400",
    "400-once",
}


class SandboxAPIError(Exception):
    """Structured API error surfaced by the Open Finance sandbox or proxy layer."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra: Dict[str, Any] = extra or {}


class ConnectionCreateRequest(BaseModel):
    institution: str = Field(..., description="Target financial institution slug.")
    username: Optional[str] = Field(default=None, description="Optional synthetic user identifier.")
    access_mode: str = Field(default="single", description="Consent mode: single or recurrent.")


class ResourceFetchRequest(BaseModel):
    connection: str = Field(..., description="Registered connection identifier.")
    save_data: bool = Field(default=True, description="Whether to persist retrieved records.")
    require_mfa: Optional[bool] = Field(
        default=None,
        description="Optional override to force or skip an HTTP 428 MFA challenge.",
    )
    challenge_type: Optional[str] = Field(
        default=None,
        description="Optional MFA challenge type override (e.g. otp_token, app_approval).",
    )


class TransactionsFetchRequest(ResourceFetchRequest):
    date_from: str = Field(..., description="Inclusive start date in YYYY-MM-DD format.")
    date_to: str = Field(..., description="Inclusive end date in YYYY-MM-DD format.")


class MfaResumeRequest(BaseModel):
    session: str = Field(..., description="Pending MFA session UUID from HTTP 428.")
    connection: str = Field(..., description="Connection identifier bound to the MFA session.")
    token: str = Field(..., description="User-supplied MFA token or approval confirmation.")
    resource: Optional[str] = Field(
        default=None,
        description="Optional resource binding verification (accounts, owners, transactions).",
    )


class FaultArmRequest(BaseModel):
    fault: str = Field(
        ...,
        description="Fault profile: 503-once, concurrent_session_limit-once, 423, etc.",
    )
    count: Optional[int] = Field(
        default=None,
        description="Number of occurrences before auto-clearing; defaults to 1 for *-once else persistent.",
    )


class OpenFinanceSandbox:
    """In-process Open Finance provider simulator with strict MFA session binding."""

    def __init__(self) -> None:
        self.connections: Dict[str, Dict[str, Any]] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.accounts: Dict[str, List[Dict[str, Any]]] = {}
        self.owners: Dict[str, List[Dict[str, Any]]] = {}
        self.transactions: Dict[str, List[Dict[str, Any]]] = {}
        self.armed_faults: List[Dict[str, Any]] = []
        self.consumed_header_faults: Set[Tuple[str, str]] = set()

    def reset(self) -> None:
        """Clear all connections, MFA sessions, stored records, and fault injection state."""
        self.connections.clear()
        self.sessions.clear()
        self.accounts.clear()
        self.owners.clear()
        self.transactions.clear()
        self.armed_faults.clear()
        self.consumed_header_faults.clear()

    def clear_faults(self) -> None:
        """Clear all armed and consumed fault injection records."""
        self.armed_faults.clear()
        self.consumed_header_faults.clear()

    def arm_fault(self, fault: str, count: Optional[int] = None) -> Dict[str, Any]:
        """Queue an explicit fault profile on subsequent sandbox calls."""
        normalized = fault.strip()
        if normalized not in SUPPORTED_FAULTS:
            raise SandboxAPIError(
                status_code=422,
                code="unsupported_simulated_fault",
                message=f"Unsupported simulated fault '{fault}'.",
                extra={"supported_faults": sorted(SUPPORTED_FAULTS)},
            )
        resolved_count = count
        if resolved_count is None:
            resolved_count = 1 if normalized.endswith("-once") else -1
        if resolved_count == 0 or resolved_count < -1:
            raise SandboxAPIError(
                status_code=422,
                code="invalid_fault_count",
                message="Fault count must be >= 1 or -1 for persistent faults.",
            )
        entry = {"fault": normalized, "remaining": resolved_count}
        self.armed_faults.append(entry)
        return dict(entry)

    def _raise_fault_error(self, fault_name: str) -> None:
        base_fault = fault_name[:-5] if fault_name.endswith("-once") else fault_name
        if base_fault == "503":
            raise SandboxAPIError(
                status_code=503,
                code="upstream_unavailable",
                message="Simulated upstream institution service interruption. Retry with backoff.",
            )
        if base_fault == "concurrent_session_limit":
            raise SandboxAPIError(
                status_code=429,
                code="concurrent_session_limit",
                message="Too many concurrent institution sessions active. Safe to retry with backoff.",
            )
        if base_fault == "423":
            raise SandboxAPIError(
                status_code=423,
                code="operational_limits_reached",
                message="Operational retrieval quota reached for this connection.",
                extra={"public_error_code": "operational_limits_reached"},
            )
        if base_fault == "429":
            raise SandboxAPIError(
                status_code=429,
                code="rate_limit_exceeded",
                message="API rate limit exceeded without retryable session semantics.",
            )
        if base_fault == "400":
            raise SandboxAPIError(
                status_code=400,
                code="invalid_request",
                message="Simulated client request validation error.",
            )
        raise SandboxAPIError(
            status_code=400,
            code="unsupported_simulated_fault",
            message=f"Unsupported simulated fault '{fault_name}'.",
            extra={"supported_faults": sorted(SUPPORTED_FAULTS)},
        )

    def maybe_trigger_fault(self, header_fault: Optional[str], request_id: str) -> None:
        """Evaluate armed faults first, then X-Simulate-Fault header fault injection."""
        if self.armed_faults:
            head = self.armed_faults[0]
            fault_name = head["fault"]
            if head["remaining"] > 0:
                head["remaining"] -= 1
                if head["remaining"] == 0:
                    self.armed_faults.pop(0)
            self._raise_fault_error(fault_name)

        if header_fault:
            normalized = header_fault.strip()
            if normalized not in SUPPORTED_FAULTS:
                raise SandboxAPIError(
                    status_code=422,
                    code="unsupported_simulated_fault",
                    message=f"Unsupported X-Simulate-Fault value '{header_fault}'.",
                    extra={"supported_faults": sorted(SUPPORTED_FAULTS)},
                )
            if normalized.endswith("-once"):
                latch_key = (request_id, normalized)
                if latch_key in self.consumed_header_faults:
                    return
                self.consumed_header_faults.add(latch_key)
            self._raise_fault_error(normalized)

    def create_connection(
        self,
        institution: str,
        username: Optional[str] = None,
        access_mode: str = "single",
    ) -> Dict[str, Any]:
        """Register a new synthetic financial institution connection."""
        clean_institution = institution.strip()
        if not clean_institution:
            raise SandboxAPIError(
                status_code=422,
                code="invalid_institution",
                message="Institution identifier must not be empty.",
            )
        if access_mode not in ("single", "recurrent"):
            raise SandboxAPIError(
                status_code=422,
                code="invalid_access_mode",
                message="access_mode must be either 'single' or 'recurrent'.",
            )

        connection_id = f"conn-{uuid.uuid4().hex[:12]}"
        record = {
            "id": connection_id,
            "institution": clean_institution,
            "username": username or "sandbox_user",
            "access_mode": access_mode,
            "status": "valid",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self.connections[connection_id] = record
        return dict(record)

    def require_connection(self, connection_id: str) -> Dict[str, Any]:
        """Ensure connection exists and return its record, or raise HTTP 404."""
        record = self.connections.get(connection_id)
        if record is None:
            raise SandboxAPIError(
                status_code=404,
                code="connection_not_found",
                message=f"Connection '{connection_id}' does not exist.",
            )
        return dict(record)

    def delete_connection(self, connection_id: str) -> None:
        """Delete a connection along with stored resource caches and pending MFA sessions."""
        if connection_id not in self.connections:
            raise SandboxAPIError(
                status_code=404,
                code="connection_not_found",
                message=f"Connection '{connection_id}' does not exist.",
            )
        del self.connections[connection_id]
        self.accounts.pop(connection_id, None)
        self.owners.pop(connection_id, None)
        self.transactions.pop(connection_id, None)
        for session_id in [k for k, v in self.sessions.items() if v["connection"] == connection_id]:
            del self.sessions[session_id]

    @staticmethod
    def parse_date_range(date_from: str, date_to: str) -> Tuple[date, date]:
        """Strictly validate YYYY-MM-DD format and ensure date_from <= date_to."""
        if not DATE_PATTERN.match(date_from) or not DATE_PATTERN.match(date_to):
            raise SandboxAPIError(
                status_code=422,
                code="invalid_date_format",
                message="date_from and date_to must strictly use YYYY-MM-DD format.",
            )
        try:
            start = date.fromisoformat(date_from)
            end = date.fromisoformat(date_to)
        except ValueError as exc:
            raise SandboxAPIError(
                status_code=422,
                code="invalid_date_value",
                message="date_from and date_to must be valid calendar dates.",
            ) from exc

        if start > end:
            raise SandboxAPIError(
                status_code=422,
                code="invalid_date_range",
                message="date_from must not be after date_to.",
            )
        return start, end

    def create_mfa_challenge(
        self,
        resource: str,
        connection: str,
        challenge_type: str = "otp_token",
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Issue or reuse an unconsumed MFA challenge bound strictly to (connection, resource)."""
        resolved_context = dict(context or {})
        for session_id, pending in self.sessions.items():
            if (
                pending["resource"] == resource
                and pending["connection"] == connection
                and pending["challenge_type"] == challenge_type
                and pending["context"] == resolved_context
                and not pending["used"]
            ):
                return {
                    "code": "mfa_challenge_required",
                    "message": f"Multi-factor authentication required before retrieving {resource}.",
                    "session": session_id,
                    "connection": connection,
                    "resource": resource,
                    "challenge_type": challenge_type,
                }

        session_id = str(uuid.uuid4())
        self.sessions[session_id] = {
            "session": session_id,
            "resource": resource,
            "connection": connection,
            "challenge_type": challenge_type,
            "used": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "context": resolved_context,
        }
        return {
            "code": "mfa_challenge_required",
            "message": f"Multi-factor authentication required before retrieving {resource}.",
            "session": session_id,
            "connection": connection,
            "resource": resource,
            "challenge_type": challenge_type,
        }

    def generate_accounts(self, connection: str, save_data: bool = True) -> List[Dict[str, Any]]:
        records = [
            {
                "id": f"{connection}-acct-001",
                "connection": connection,
                "name": "Checking Account Sandbox",
                "category": "CHECKING_ACCOUNT",
                "currency": "BRL",
                "balance": {"current": 4250.75, "available": 3900.00},
                "last_accessed_at": datetime.now(timezone.utc).isoformat(),
            },
            {
                "id": f"{connection}-acct-002",
                "connection": connection,
                "name": "Savings Reserve Sandbox",
                "category": "SAVINGS_ACCOUNT",
                "currency": "BRL",
                "balance": {"current": 12800.00, "available": 12800.00},
                "last_accessed_at": datetime.now(timezone.utc).isoformat(),
            },
        ]
        if save_data:
            self.accounts[connection] = records
        return records

    def generate_owners(self, connection: str, save_data: bool = True) -> List[Dict[str, Any]]:
        records = [
            {
                "id": f"{connection}-owner-001",
                "connection": connection,
                "display_name": "Alex Rivera",
                "email": "alex.rivera@sandbox.example",
                "phone_number": "+55 11 90000-0101",
                "address": "Av. Paulista, 1000, Sao Paulo, SP",
                "document_id": {"type": "CPF", "number": "***.***.***-00"},
            }
        ]
        if save_data:
            self.owners[connection] = records
        return records

    def generate_transactions(
        self,
        connection: str,
        date_from: str,
        date_to: str,
        save_data: bool = True,
    ) -> List[Dict[str, Any]]:
        start, end = self.parse_date_range(date_from, date_to)
        today = date.today()
        entries = [
            (4, "Payroll Deposit", 6200.00, "INFLOW", "SALARY", "PROCESSED"),
            (3, "Grocery Market", -214.50, "OUTFLOW", "GROCERIES", "PROCESSED"),
            (2, "Cloud Streaming", -39.90, "OUTFLOW", "ENTERTAINMENT", "PROCESSED"),
            (1, "Instant Transfer Received", 350.00, "INFLOW", "TRANSFER", "PROCESSED"),
            (0, "Electric Utility", -168.40, "OUTFLOW", "UTILITIES", "PENDING"),
        ]
        all_rows = [
            {
                "id": f"{connection}-tx-{idx}",
                "connection": connection,
                "value_date": str(today - timedelta(days=days_ago)),
                "description": description,
                "amount": amount,
                "currency": "BRL",
                "type": tx_type,
                "category": category,
                "status": status,
            }
            for idx, (days_ago, description, amount, tx_type, category, status)
            in enumerate(entries, start=1)
        ]
        filtered = [
            row
            for row in all_rows
            if start <= date.fromisoformat(row["value_date"]) <= end
        ]
        if save_data:
            self.transactions[connection] = filtered
        return filtered

    def filter_stored_transactions(
        self,
        connection: str,
        date_from: str,
        date_to: str,
    ) -> List[Dict[str, Any]]:
        self.require_connection(connection)
        start, end = self.parse_date_range(date_from, date_to)
        stored = self.transactions.get(connection, [])
        return [
            row
            for row in stored
            if start <= date.fromisoformat(row["value_date"]) <= end
        ]

    def resume_mfa(
        self,
        session: str,
        connection: str,
        token: str,
        resource: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Validate session/connection/resource binding, prevent replay, and return materialized data."""
        if not token.strip():
            raise SandboxAPIError(
                status_code=422,
                code="invalid_mfa_token",
                message="A non-empty MFA token is required to resume the challenge.",
            )

        pending = self.sessions.get(session)
        if pending is None:
            raise SandboxAPIError(
                status_code=400,
                code="unknown_mfa_session",
                message=f"Unknown MFA session '{session}'.",
            )

        if pending["connection"] != connection:
            raise SandboxAPIError(
                status_code=409,
                code="session_connection_mismatch",
                message="MFA session does not belong to the specified connection.",
                extra={"expected_connection": pending["connection"], "received_connection": connection},
            )

        if resource is not None and pending["resource"] != resource:
            raise SandboxAPIError(
                status_code=409,
                code="session_resource_mismatch",
                message="MFA session does not match the specified resource.",
                extra={"expected_resource": pending["resource"], "received_resource": resource},
            )

        if pending["used"]:
            raise SandboxAPIError(
                status_code=409,
                code="session_already_used",
                message="MFA session has already been consumed and cannot be replayed.",
            )

        self.require_connection(connection)
        pending["used"] = True

        target_resource = pending["resource"]
        ctx = pending["context"]
        save_data = bool(ctx.get("save_data", True))

        if target_resource == "accounts":
            data = self.generate_accounts(connection, save_data=save_data)
        elif target_resource == "owners":
            data = self.generate_owners(connection, save_data=save_data)
        elif target_resource == "transactions":
            data = self.generate_transactions(
                connection=connection,
                date_from=ctx["date_from"],
                date_to=ctx["date_to"],
                save_data=save_data,
            )
        else:
            raise SandboxAPIError(
                status_code=400,
                code="unsupported_mfa_resource",
                message=f"Unsupported MFA resource '{target_resource}'.",
            )

        return {
            "status": "completed",
            "session": session,
            "connection": connection,
            "resource": target_resource,
            "challenge_type": pending["challenge_type"],
            "data": data,
        }


sandbox = OpenFinanceSandbox()


def reset_sandbox_state() -> None:
    """Reset the module-level sandbox state (for tests and local demos)."""
    sandbox.reset()
