"""Azure DevOps pool, agent, queue, and project discovery."""

from __future__ import annotations

from typing import Any

from .classify import pool_info_from_api
from .http import AdoHttpError, AzureDevOpsHttpClient
from .models import PoolInfo

API_VERSION = "7.1"


async def list_projects(client: AzureDevOpsHttpClient, org: str) -> list[dict[str, Any]]:
    """List well-formed projects in an Azure DevOps organisation."""
    url = f"https://dev.azure.com/{org}/_apis/projects"
    params = {"api-version": API_VERSION, "$top": "1000", "stateFilter": "wellFormed"}
    projects: list[dict[str, Any]] = []
    async for body, _ in client.get_continuation_pages(url, params=params):
        projects.extend(body.get("value", []))
    return projects


async def fetch_pool_agents(client: AzureDevOpsHttpClient, org: str, pool_id: int) -> list[dict[str, Any]]:
    """Fetch agents and capabilities for a pool; return empty on permission-denied hosted pools."""
    url = f"https://dev.azure.com/{org}/_apis/distributedtask/pools/{pool_id}/agents"
    try:
        body, _ = await client.get_json(url, params={"includeCapabilities": "true", "api-version": API_VERSION})
    except AdoHttpError as exc:
        if exc.status_code in {401, 403, 404}:
            return []
        raise
    return body.get("value", []) if isinstance(body, dict) else []


async def fetch_pools(client: AzureDevOpsHttpClient, org: str, enrich_agents: bool = True) -> dict[int, PoolInfo]:
    """Fetch and classify all agent pools for an organisation."""
    url = f"https://dev.azure.com/{org}/_apis/distributedtask/pools"
    body, _ = await client.get_json(url, params={"api-version": API_VERSION})
    pools: dict[int, PoolInfo] = {}
    for pool in body.get("value", []):
        agents = await fetch_pool_agents(client, org, int(pool["id"])) if enrich_agents else []
        info = pool_info_from_api(org, pool, agents)
        pools[info.id] = info
    return pools


async def fetch_project_queues(client: AzureDevOpsHttpClient, org: str, project: str) -> dict[int, dict[str, Any]]:
    """Fetch a project's agent queues keyed by queue id."""
    url = f"https://dev.azure.com/{org}/{project}/_apis/distributedtask/queues"
    body, _ = await client.get_json(url, params={"api-version": API_VERSION})
    return {int(queue["id"]): queue for queue in body.get("value", []) if "id" in queue}
