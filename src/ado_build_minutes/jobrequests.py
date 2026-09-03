"""Pool jobrequests collector and parsing helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from .http import AdoHttpError, AzureDevOpsHttpClient
from .models import CollectionResult, Failure, PoolInfo, UsageRecord, duration_minutes, month_key, parse_ado_datetime
from .pools import API_VERSION, fetch_pools


def parse_jobrequests_payload(payload: Any) -> list[dict[str, Any]]:
    """Parse jobrequests payloads defensively, including the bare-array $top shape."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        value = payload.get("value", [])
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _nested_name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("name") or value.get("Name")
        return str(name) if name is not None else None
    return str(value) if value is not None else None


def jobrequest_to_record(org: str, pool: PoolInfo, request: dict[str, Any]) -> UsageRecord | None:
    """Convert a TaskAgentJobRequest payload to a normalized UsageRecord."""
    if request.get("result") is None:
        return None
    finish = request.get("finishTime") or request.get("FinishedDate") or request.get("finishDate")
    receive = request.get("receiveTime") or request.get("StartedDate") or request.get("startTime")
    if not finish or not receive or not request.get("assignTime"):
        return None
    minutes = duration_minutes(receive, finish)
    if minutes is None:
        return None
    assign = request.get("assignTime")
    queue = request.get("queueTime") or request.get("QueuedDate")
    queue_minutes = duration_minutes(queue, assign)
    data = request.get("data") or {}
    agent_spec = request.get("agentSpecification") or {}
    reserved_agent = request.get("reservedAgent") or {}
    definition = request.get("definition") or {}
    owner = request.get("owner") or {}
    image = agent_spec.get("identifier") or agent_spec.get("Identifier")
    return UsageRecord(
        source="jobrequests",
        org=org,
        project=_nested_name(request.get("project")) or data.get("System.TeamProject"),
        pipeline=_nested_name(definition),
        runner_type=pool.runner_type,
        pool_id=pool.id,
        pool_name=pool.name,
        image=str(image) if image else None,
        minutes=minutes,
        jobs=1,
        month=month_key(finish or receive),
        started_at=receive,
        finished_at=finish,
        queue_minutes=queue_minutes,
        result=request.get("result"),
        request_id=request.get("requestId") or request.get("id"),
        definition=_nested_name(definition),
        owner=_nested_name(owner),
        plan_type=request.get("planType"),
        parallelism_tag=data.get("ParallelismTag") or data.get("System.ParallelismTag"),
        extra={
            "reserved_agent_name": reserved_agent.get("name"),
            "reserved_agent_os": reserved_agent.get("osDescription"),
        },
    )


async def collect_jobrequests(
    client: AzureDevOpsHttpClient,
    orgs: list[str],
    start: datetime,
    end: datetime,
    completed_request_count: int = 1000,
) -> CollectionResult:
    """Collect recent per-job records from the semi-documented pool jobrequests endpoint."""
    result = CollectionResult()
    result.expected_orgs = list(orgs)
    for org in orgs:
        try:
            pools = await fetch_pools(client, org, enrich_agents=True)
        except AdoHttpError as exc:
            result.failures.append(Failure("jobrequests", org, None, exc.status_code, exc.body or str(exc)))
            continue

        async def collect_pool(pool: PoolInfo) -> tuple[list[UsageRecord], list[str], list[Failure]]:
            url = f"https://dev.azure.com/{org}/_apis/distributedtask/pools/{pool.id}/jobrequests"
            params = {"api-version": API_VERSION, "completedRequestCount": str(completed_request_count)}
            try:
                payload, _ = await client.get_json(url, params=params)
            except AdoHttpError as exc:
                return [], [], [Failure("jobrequests", org, None, exc.status_code, f"pool {pool.name}: {exc.body or str(exc)}")]
            parsed = parse_jobrequests_payload(payload)
            oldest: datetime | None = None
            records: list[UsageRecord] = []
            warnings: list[str] = []
            kept = 0
            for item in parsed:
                record = jobrequest_to_record(org, pool, item)
                if record is None:
                    continue
                finish_dt = parse_ado_datetime(record.finished_at) or parse_ado_datetime(record.started_at)
                if finish_dt:
                    oldest = finish_dt if oldest is None else min(oldest, finish_dt)
                    if start <= finish_dt <= end:
                        records.append(record)
                        kept += 1
            if parsed and oldest and oldest > start:
                warnings.append(
                    f"{org}/{pool.name}: jobrequests returned only back to {oldest.date()}, "
                    "but the requested range starts earlier; retention/capping may hide older jobs."
                )
            if not parsed:
                warnings.append(f"{org}/{pool.name}: jobrequests returned no completed requests.")
            return records, warnings, []

        pool_results = await asyncio.gather(*(collect_pool(pool) for pool in pools.values()))
        result.mark_source_covered("jobrequests", org)
        for records, warnings, failures in pool_results:
            result.records.extend(records)
            result.warnings.extend(warnings)
            result.failures.extend(failures)
    return result
