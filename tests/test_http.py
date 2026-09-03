import asyncio

import pytest

from ado_build_minutes.http import AdoHttpError, AzureDevOpsHttpClient
from ado_build_minutes.http import compute_backoff_delay, proactive_delay_seconds


def test_backoff_respects_retry_after_header():
    decision = compute_backoff_delay(429, 1, {"Retry-After": "17"}, random_fraction=0)
    assert decision.should_retry is True
    assert decision.delay_seconds == 17
    assert decision.reason == "Retry-After"


def test_backoff_uses_exponential_delay_with_jitter():
    decision = compute_backoff_delay(503, 3, {}, random_fraction=0.5)
    assert decision.should_retry is True
    assert decision.delay_seconds == 6.0
    assert "jitter" in decision.reason


def test_backoff_stops_after_max_attempts_and_non_retryable():
    assert compute_backoff_delay(429, 8, {}).should_retry is False
    assert compute_backoff_delay(404, 1, {}).should_retry is False


def test_proactive_delay_honours_headers():
    assert proactive_delay_seconds({"X-RateLimit-Delay": "4"}) == 4
    assert proactive_delay_seconds({"X-RateLimit-Remaining": "3", "X-RateLimit-Cost": "2"}) == 3


def test_continuation_pagination_rejects_unchanged_token():
    client = AzureDevOpsHttpClient.__new__(AzureDevOpsHttpClient)

    async def get_json(url, params=None):
        return {"value": []}, {"x-ms-continuationtoken": "same"}

    client.get_json = get_json

    async def consume():
        pages = []
        async for page, token in client.get_continuation_pages("https://dev.azure.com/org/_apis/projects", max_pages=5):
            pages.append((page, token))
        return pages

    with pytest.raises(AdoHttpError, match="unchanged continuation token"):
        asyncio.run(consume())
