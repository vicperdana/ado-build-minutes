"""HTTP client with Azure DevOps retry, throttling, and pagination support."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import logging
import random
import time
from typing import Any, AsyncIterator, Mapping

try:
    import httpx
except ImportError:  # pragma: no cover - dependency-free tests cover pure helpers only.
    httpx = None  # type: ignore[assignment]

from .auth import AuthProvider, redact_secret

RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass
class BackoffDecision:
    """Retry/backoff decision returned by compute_backoff_delay."""

    should_retry: bool
    delay_seconds: float
    reason: str


class AdoHttpError(RuntimeError):
    """HTTP error raised after retries are exhausted."""

    def __init__(self, status_code: int, url: str, message: str, body: str | None = None) -> None:
        super().__init__(f"HTTP {status_code} for {url}: {message}")
        self.status_code = status_code
        self.url = url
        self.body = body or ""


def _parse_retry_after(value: str | None, now: float | None = None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, dt.timestamp() - (time.time() if now is None else now))


def compute_backoff_delay(
    status_code: int,
    attempt: int,
    headers: Mapping[str, str],
    max_attempts: int = 8,
    random_fraction: float | None = None,
) -> BackoffDecision:
    """Compute Azure DevOps retry delay with Retry-After, exponential backoff, and jitter."""
    if attempt >= max_attempts:
        return BackoffDecision(False, 0.0, "max attempts reached")
    if status_code not in RETRY_STATUS_CODES:
        return BackoffDecision(False, 0.0, "not retryable")
    retry_after = _parse_retry_after(headers.get("Retry-After"))
    if retry_after is not None:
        return BackoffDecision(True, retry_after, "Retry-After")
    base = min(60.0, 2.0 ** max(0, attempt - 1))
    jitter = (random.random() if random_fraction is None else random_fraction) * base
    return BackoffDecision(True, base + jitter, "exponential backoff with jitter")


def proactive_delay_seconds(headers: Mapping[str, str]) -> float:
    """Return a conservative delay when Azure DevOps rate-limit headers ask us to slow down."""
    explicit_delay = headers.get("X-RateLimit-Delay")
    if explicit_delay:
        try:
            return min(float(explicit_delay), 30.0)
        except ValueError:
            return 0.0
    remaining = headers.get("X-RateLimit-Remaining")
    cost = headers.get("X-RateLimit-Cost")
    try:
        remaining_f = float(remaining) if remaining is not None else None
        cost_f = float(cost) if cost is not None else 1.0
    except ValueError:
        return 0.0
    if remaining_f is not None and remaining_f <= max(10.0, cost_f * 2.0):
        return min(30.0, 1.0 + cost_f)
    return 0.0


class AzureDevOpsHttpClient:
    """Shared async httpx client for Azure DevOps REST and Analytics APIs."""

    def __init__(self, auth: AuthProvider, user_agent: str, concurrency: int = 6, max_attempts: int = 8) -> None:
        if httpx is None:
            raise RuntimeError("httpx is required for API calls. Install with: pip install -e '.[runtime]'")
        self.auth = auth
        self.user_agent = user_agent
        self.semaphore = asyncio.Semaphore(max(1, min(concurrency, 20)))
        self.max_attempts = max_attempts
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0), follow_redirects=True)
        self._log = logging.getLogger(__name__)

    async def __aenter__(self) -> "AzureDevOpsHttpClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Send an authenticated request with Azure DevOps retry/backoff semantics."""
        request_headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if headers:
            request_headers.update(headers)
        last_body = ""
        for attempt in range(1, self.max_attempts + 1):
            request_headers["Authorization"] = self.auth.authorization_header(force_refresh=False)
            try:
                async with self.semaphore:
                    self._log.debug("%s %s", method.upper(), url)
                    response = await self._client.request(method, url, params=params, json=json, headers=request_headers)
            except httpx.RequestError as exc:
                if attempt >= self.max_attempts:
                    raise AdoHttpError(0, url, redact_secret(str(exc))) from exc
                delay = min(60.0, 2.0 ** (attempt - 1) + random.random())
                self._log.warning("Request error; retrying in %.1fs: %s", delay, redact_secret(str(exc)))
                await asyncio.sleep(delay)
                continue

            delay = proactive_delay_seconds(response.headers)
            if delay:
                self._log.info("Azure DevOps rate-limit headers requested %.1fs slowdown", delay)
                await asyncio.sleep(delay)

            if response.status_code == 401 and attempt < self.max_attempts:
                request_headers["Authorization"] = self.auth.authorization_header(force_refresh=True)
                await asyncio.sleep(0.5)
                continue

            if response.status_code < 400:
                return response

            last_body = response.text[:1000]
            decision = compute_backoff_delay(response.status_code, attempt, response.headers, self.max_attempts)
            if decision.should_retry:
                self._log.warning(
                    "HTTP %s from Azure DevOps; retrying in %.1fs (%s)",
                    response.status_code,
                    decision.delay_seconds,
                    decision.reason,
                )
                await asyncio.sleep(decision.delay_seconds)
                continue

            raise AdoHttpError(response.status_code, str(response.url), response.reason_phrase, redact_secret(last_body))
        raise AdoHttpError(-1, url, "Retries exhausted", redact_secret(last_body))

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> tuple[Any, httpx.Headers]:
        """GET JSON and return the decoded body plus response headers."""
        response = await self.request("GET", url, params=params)
        if not response.content:
            return {}, response.headers
        return response.json(), response.headers

    async def post_json(self, url: str, *, payload: Any, params: dict[str, Any] | None = None) -> tuple[Any, httpx.Headers]:
        """POST JSON and return the decoded body plus response headers."""
        response = await self.request("POST", url, params=params, json=payload, headers={"Content-Type": "application/json"})
        if not response.content:
            return {}, response.headers
        return response.json(), response.headers

    async def get_continuation_pages(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        continuation: str | None = None,
        max_pages: int = 10_000,
    ) -> AsyncIterator[tuple[Any, str | None]]:
        """Yield JSON pages following the x-ms-continuationtoken response header pattern."""
        next_token = continuation
        seen_tokens: set[str] = set()
        page_count = 0
        while True:
            if page_count >= max_pages:
                raise AdoHttpError(0, url, f"Pagination exceeded max_pages={max_pages}")
            current_token = next_token
            if current_token:
                if current_token in seen_tokens:
                    raise AdoHttpError(0, url, f"Pagination made no forward progress; repeated continuation token {current_token!r}")
                seen_tokens.add(current_token)
            page_params = dict(params or {})
            if current_token:
                page_params["continuationToken"] = current_token
            body, headers = await self.get_json(url, params=page_params)
            next_token = headers.get("x-ms-continuationtoken") or headers.get("X-MS-ContinuationToken")
            page_count += 1
            yield body, next_token
            if not next_token:
                break
            if next_token == current_token:
                raise AdoHttpError(0, url, f"Pagination made no forward progress; unchanged continuation token {next_token!r}")
