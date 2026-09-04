"""Regression tests for Azure DevOps api-version requirements."""

import asyncio

from ado_build_minutes.billing import collect_billing
from ado_build_minutes.doctor import CONNECTION_DATA_API_VERSION
from ado_build_minutes.pools import DATA_PROVIDER_API_VERSION


class RecordingClient:
    """Captures the params passed to each request."""

    def __init__(self):
        self.posts: list[tuple[str, dict | None]] = []
        self.gets: list[tuple[str, dict | None]] = []

    async def post_json(self, url, *, payload, params=None):
        self.posts.append((url, params))
        return {"dataProviders": {}}, {}

    async def get_json(self, url, *, params=None):
        self.gets.append((url, params))
        return {}, {}


def test_data_provider_post_supplies_api_version():
    """The Contribution data-provider endpoint returns HTTP 400 without an api-version."""
    client = RecordingClient()
    asyncio.run(collect_billing(client, ["org-a"]))

    provider_posts = [(url, params) for url, params in client.posts if "dataProviders/query" in url]
    assert provider_posts, "expected a data-provider query POST"
    for _, params in provider_posts:
        assert (params or {}).get("api-version") == DATA_PROVIDER_API_VERSION


def test_connection_data_uses_preview_api_version():
    """connectionData is preview-only; a plain '7.1' api-version returns HTTP 400."""
    assert CONNECTION_DATA_API_VERSION.endswith("-preview.1")
