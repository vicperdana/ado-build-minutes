"""Runner-type classification helpers."""

from __future__ import annotations

from typing import Any

from .models import PoolInfo


DEPLOYMENT_GROUP = "deployment_group"
GITHUB_HOSTED = "github_hosted"
MICROSOFT_HOSTED = "microsoft_hosted"
VMSS_ELASTIC_POOL = "vmss_elastic_pool"
MANAGED_DEVOPS_POOL = "managed_devops_pool"
SELF_HOSTED = "self_hosted"
UNKNOWN = "unknown"


def _options_contains_elastic_pool(options: Any) -> bool:
    if options is None:
        return False
    if isinstance(options, list):
        return any(str(item).lower() == "elasticpool" for item in options)
    if isinstance(options, dict):
        return any(str(key).lower() == "elasticpool" or str(value).lower() == "elasticpool" for key, value in options.items())
    return "elasticpool" in str(options).replace("_", "").lower()


def classify_pool(pool: dict[str, Any]) -> str:
    """Classify an Azure DevOps TaskAgentPool using the research decision tree."""
    if str(pool.get("poolType") or "").lower() == "deployment":
        return DEPLOYMENT_GROUP
    if pool.get("isHosted") is True:
        if str(pool.get("name") or "").casefold() == "github-hosted agents".casefold():
            return GITHUB_HOSTED
        return MICROSOFT_HOSTED
    if _options_contains_elastic_pool(pool.get("options")):
        return VMSS_ELASTIC_POOL
    if pool.get("agentCloudId") is not None:
        return MANAGED_DEVOPS_POOL
    return SELF_HOSTED


def pool_info_from_api(org: str, pool: dict[str, Any], agents: list[dict[str, Any]] | None = None) -> PoolInfo:
    """Convert an Azure DevOps pool payload and optional agents into PoolInfo."""
    agents = agents or []
    os_names = sorted({str((agent.get("systemCapabilities") or {}).get("Agent.OS")) for agent in agents if (agent.get("systemCapabilities") or {}).get("Agent.OS")})
    os_desc = sorted({str(agent.get("osDescription")) for agent in agents if agent.get("osDescription")})
    return PoolInfo(
        org=org,
        id=int(pool["id"]),
        name=str(pool.get("name") or pool["id"]),
        runner_type=classify_pool(pool),
        is_hosted=pool.get("isHosted"),
        pool_type=pool.get("poolType"),
        options=pool.get("options"),
        agent_cloud_id=pool.get("agentCloudId"),
        size=pool.get("size"),
        os_names=tuple(os_names),
        os_descriptions=tuple(os_desc),
    )
