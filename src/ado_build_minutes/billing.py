"""Unsupported Azure DevOps billing counter collector."""

from __future__ import annotations

from typing import Any

from .classify import CAPACITY_NOT_MINUTES, MICROSOFT_HOSTED
from .http import AdoHttpError, AzureDevOpsHttpClient
from .models import CollectionResult, Failure, UsageRecord
from .pools import DATA_PROVIDER_API_VERSION


def ci_get(mapping: Any, key: str) -> Any:
    """Case-insensitive dictionary lookup with None safety."""
    if not isinstance(mapping, dict):
        return None
    for candidate, value in mapping.items():
        if str(candidate).casefold() == key.casefold():
            return value
    return None


def extract_billing_minutes(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract hosted-agent minute counters from the unsupported UI data-provider payload."""
    providers = ci_get(payload, "dataProviders") or {}
    provider = ci_get(providers, "ms.vss-build-web.build-queue-hub-data-provider") or {}
    details = ci_get(provider, "taskHubLicenseDetails") or ci_get(provider, "TaskHubLicenseDetails") or {}
    return {
        "hosted_used": ci_get(details, "hostedAgentMinutesUsedCount"),
        "hosted_free": ci_get(details, "hostedAgentMinutesFreeCount"),
        "raw_details": details,
    }


async def collect_billing(client: AzureDevOpsHttpClient, orgs: list[str]) -> CollectionResult:
    """Collect current billing-period hosted minutes from an unsupported endpoint."""
    result = CollectionResult()
    result.expected_orgs = list(orgs)
    payload = {"contributionIds": ["ms.vss-build-web.build-queue-hub-data-provider"]}
    for org in orgs:
        url = f"https://dev.azure.com/{org}/_apis/Contribution/dataProviders/query"
        try:
            body, _ = await client.post_json(
                url,
                payload=payload | {"dataProviderContext": {"properties": {}}},
                params={"api-version": DATA_PROVIDER_API_VERSION},
            )
            result.mark_source_covered("billing_unsupported", org)
            counters = extract_billing_minutes(body if isinstance(body, dict) else {})
            used = counters.get("hosted_used")
            if used is not None:
                result.records.append(
                    UsageRecord(
                        source="billing_unsupported",
                        org=org,
                        runner_type=MICROSOFT_HOSTED,
                        minutes=float(used),
                        jobs=0,
                        granularity="current_billing_period",
                        unsupported=True,
                        extra={"hosted_free_minutes": counters.get("hosted_free")},
                    )
                )
            else:
                result.warnings.append(f"{org}: unsupported billing endpoint returned no hostedAgentMinutesUsedCount.")
        except AdoHttpError as exc:
            result.failures.append(Failure("billing_unsupported", org, None, exc.status_code, exc.body or str(exc)))
        resource_url = f"https://dev.azure.com/{org}/_apis/build/resourceusage"
        try:
            body, _ = await client.get_json(resource_url, params={"api-version": "7.1-preview.2"})
            result.mark_source_covered("billing_resourceusage_capacity", org)
            result.records.append(
                UsageRecord(
                    source="billing_resourceusage_capacity",
                    org=org,
                    runner_type=CAPACITY_NOT_MINUTES,
                    minutes=0.0,
                    jobs=0,
                    granularity="capacity_snapshot",
                    unsupported=True,
                    extra={"resource_usage": body},
                )
            )
        except AdoHttpError as exc:
            result.failures.append(Failure("billing_resourceusage", org, None, exc.status_code, exc.body or str(exc)))
    result.warnings.append("Billing source uses unsupported UI data-provider endpoints and may break; current billing period only.")
    return result
