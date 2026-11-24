"""Shared async HTTP client with selective retry and non-2xx request_id logging."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

SleepFn = Callable[[float], Awaitable[None]]
JitterFn = Callable[[float], float]
ClientFactory = Callable[[], httpx.AsyncClient]

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 30.0
DEFAULT_BASE_DELAY = 3.0
DEFAULT_BACKOFF_FACTOR = 2.0
DEFAULT_MAX_RETRIES = 5
DEFAULT_MAX_RETRY_AFTER_SECONDS = 60.0

_client: Optional[httpx.AsyncClient] = None
_client_factory: Optional[ClientFactory] = None


def configure_client_factory(factory: Optional[ClientFactory]) -> None:
    """Register a factory used to construct the shared httpx.AsyncClient."""
    global _client_factory
    _client_factory = factory


def init_client(
    base_url: Optional[str] = None,
    timeout: Optional[float] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    force: bool = False,
) -> httpx.AsyncClient:
    """Initialize and return the shared httpx.AsyncClient instance."""
    global _client
    if _client is not None and not _client.is_closed and not force:
        return _client

    if _client is None or _client.is_closed or force:
        if _client_factory is not None and base_url is None and transport is None:
            _client = _client_factory()
        else:
            resolved_url = base_url or os.getenv("OPEN_FINANCE_BASE_URL", DEFAULT_BASE_URL)
            resolved_timeout = (
                timeout
                if timeout is not None
                else float(os.getenv("OPEN_FINANCE_TIMEOUT", str(DEFAULT_TIMEOUT)))
            )
            client_kwargs: Dict[str, Any] = {
                "base_url": resolved_url,
                "timeout": resolved_timeout,
            }
            if transport is not None:
                client_kwargs["transport"] = transport
            _client = httpx.AsyncClient(**client_kwargs)
    return _client


def get_client() -> httpx.AsyncClient:
    """Return the active shared httpx.AsyncClient, creating it lazily if needed."""
    global _client
    if _client is None or _client.is_closed:
        return init_client()
    return _client


def set_client(client: Optional[httpx.AsyncClient]) -> None:
    """Override the shared httpx.AsyncClient (primarily for deterministic testing)."""
    global _client
    _client = client


async def close_client() -> None:
    """Close and release the shared httpx.AsyncClient during shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None


def _iter_error_items(response: httpx.Response) -> List[Dict[str, Any]]:
    """Extract structured dict items from dict, list, or nested detail payloads."""
    try:
        body = response.json()
    except Exception:
        return []

    items: List[Dict[str, Any]] = []
    if isinstance(body, dict):
        items.append(body)
        detail = body.get("detail")
        if isinstance(detail, dict):
            items.append(detail)
        elif isinstance(detail, list):
            items.extend(entry for entry in detail if isinstance(entry, dict))
    elif isinstance(body, list):
        items.extend(entry for entry in body if isinstance(entry, dict))
    return items


def extract_request_id(response: httpx.Response) -> Optional[str]:
    """Resolve request_id from JSON payload first, falling back to HTTP headers."""
    for item in _iter_error_items(response):
        req_id = item.get("request_id")
        if req_id:
            return str(req_id)

    header_id = response.headers.get("X-Request-ID") or response.headers.get("x-request-id")
    if header_id:
        return str(header_id)

    try:
        if response.request is not None:
            outbound_id = response.request.headers.get("X-Request-ID")
            if outbound_id:
                return str(outbound_id)
    except RuntimeError:
        pass
    return None


def extract_error_codes(response: httpx.Response) -> List[str]:
    """Return all error codes declared in a structured Open Finance error body."""
    codes: List[str] = []
    for item in _iter_error_items(response):
        code = item.get("code")
        if isinstance(code, str) and code:
            codes.append(code)
    return codes


def parse_retry_after_seconds(
    response: httpx.Response,
    max_seconds: float = DEFAULT_MAX_RETRY_AFTER_SECONDS,
) -> Optional[float]:
    """Parse numeric Retry-After header seconds bounded cleanly to ``[0, max_seconds]``."""
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        parsed = float(str(raw).strip())
    except ValueError:
        return None
    if parsed < 0:
        return None
    return min(parsed, float(max_seconds))


def should_retry(response: httpx.Response) -> bool:
    """Retry on 5xx responses, and on 4xx ONLY when code == 'too_many_sessions'."""
    if response.status_code >= 500:
        return True
    if 400 <= response.status_code < 500:
        return "too_many_sessions" in extract_error_codes(response)
    return False


def log_non_2xx(response: httpx.Response) -> Optional[str]:
    """Log every non-2xx response with status, path, error code, and request_id."""
    request_id = extract_request_id(response)
    codes = extract_error_codes(response)
    primary_code = codes[0] if codes else None
    try:
        path = response.url.path
        method = response.request.method if response.request is not None else "-"
    except RuntimeError:
        path = "-"
        method = "-"

    log_fn = logger.warning if response.status_code < 400 else logger.error
    log_fn(
        "Open Finance non-2xx response: status=%s method=%s path=%s request_id=%s code=%s",
        response.status_code,
        method,
        path,
        request_id or "none",
        primary_code or "none",
    )
    return request_id


class ResilientOpenFinanceClient:
    """Async Open Finance client with configurable exponential backoff and selective retry."""

    def __init__(
        self,
        client: Optional[httpx.AsyncClient] = None,
        base: float = DEFAULT_BASE_DELAY,
        factor: float = DEFAULT_BACKOFF_FACTOR,
        max_retries: int = DEFAULT_MAX_RETRIES,
        sleep_fn: Optional[SleepFn] = None,
        respect_retry_after: bool = True,
        max_retry_after_seconds: float = DEFAULT_MAX_RETRY_AFTER_SECONDS,
        jitter_fn: Optional[JitterFn] = None,
    ) -> None:
        if base < 0:
            raise ValueError("base must be >= 0")
        if factor <= 0:
            raise ValueError("factor must be > 0")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds must be >= 0")
        self._client = client
        self.base = base
        self.factor = factor
        self.max_retries = max_retries
        self.sleep_fn: SleepFn = sleep_fn if sleep_fn is not None else asyncio.sleep
        self.respect_retry_after = respect_retry_after
        self.max_retry_after_seconds = float(max_retry_after_seconds)
        self.jitter_fn = jitter_fn

    def _compute_delay(self, attempt: int, response: Optional[httpx.Response] = None) -> float:
        delay = self.base * (self.factor ** attempt)
        if response is not None and self.respect_retry_after:
            retry_after = parse_retry_after_seconds(
                response, max_seconds=self.max_retry_after_seconds
            )
            if retry_after is not None:
                delay = max(delay, retry_after)
        if self.jitter_fn is not None:
            delay = max(0.0, float(self.jitter_fn(delay)))
        return delay

    def _active_client(self) -> httpx.AsyncClient:
        return self._client if self._client is not None else get_client()

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Execute an HTTP request, applying selective retry with exponential backoff."""
        active_client = self._active_client()
        last_response: Optional[httpx.Response] = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await active_client.request(method, path, **kwargs)
            except httpx.RequestError as exc:
                logger.error(
                    "Open Finance transport error on attempt %d/%d (%s %s): %s",
                    attempt + 1,
                    self.max_retries + 1,
                    method,
                    path,
                    exc,
                )
                if attempt == self.max_retries:
                    raise
                delay = self._compute_delay(attempt)
                await self.sleep_fn(delay)
                continue

            if not (200 <= response.status_code < 300):
                log_non_2xx(response)

            if should_retry(response) and attempt < self.max_retries:
                delay = self._compute_delay(attempt, response=response)
                logger.warning(
                    "Retrying Open Finance request in %.3fs (attempt %d/%d, status=%s, path=%s)",
                    delay,
                    attempt + 1,
                    self.max_retries,
                    response.status_code,
                    path,
                )
                await self.sleep_fn(delay)
                last_response = response
                continue

            return response

        return last_response  # type: ignore[return-value]

    async def get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        return await self.request("GET", path, params=params, headers=headers)

    async def post(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        return await self.request("POST", path, json=json, headers=headers)

    async def patch(
        self,
        path: str,
        json: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        return await self.request("PATCH", path, json=json, headers=headers)

    async def delete(
        self,
        path: str,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        return await self.request("DELETE", path, headers=headers)


async def request_with_retry(
    method: str,
    path: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
    base: float = DEFAULT_BASE_DELAY,
    factor: float = DEFAULT_BACKOFF_FACTOR,
    max_retries: int = DEFAULT_MAX_RETRIES,
    sleep_fn: Optional[SleepFn] = None,
    respect_retry_after: bool = True,
    max_retry_after_seconds: float = DEFAULT_MAX_RETRY_AFTER_SECONDS,
    jitter_fn: Optional[JitterFn] = None,
    **kwargs: Any,
) -> httpx.Response:
    """Execute an Open Finance request using the shared or provided AsyncClient."""
    resilient = ResilientOpenFinanceClient(
        client=client,
        base=base,
        factor=factor,
        max_retries=max_retries,
        sleep_fn=sleep_fn,
        respect_retry_after=respect_retry_after,
        max_retry_after_seconds=max_retry_after_seconds,
        jitter_fn=jitter_fn,
    )
    return await resilient.request(method, path, **kwargs)


async def api_get(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    **retry_kwargs: Any,
) -> httpx.Response:
    return await request_with_retry("GET", path, params=params, headers=headers, **retry_kwargs)


async def api_post(
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    **retry_kwargs: Any,
) -> httpx.Response:
    return await request_with_retry("POST", path, json=payload, headers=headers, **retry_kwargs)


async def api_patch(
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    **retry_kwargs: Any,
) -> httpx.Response:
    return await request_with_retry("PATCH", path, json=payload, headers=headers, **retry_kwargs)


async def api_delete(
    path: str,
    headers: Optional[Dict[str, str]] = None,
    **retry_kwargs: Any,
) -> httpx.Response:
    return await request_with_retry("DELETE", path, headers=headers, **retry_kwargs)
