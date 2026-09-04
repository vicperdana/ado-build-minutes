"""Analytics OData collectors."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from .classify import CAPACITY_NOT_MINUTES, UNKNOWN
from .http import AdoHttpError, AzureDevOpsHttpClient
from .models import CollectionResult, Failure, PoolInfo, UsageRecord, month_key
from .pools import fetch_pools, list_projects

ODATA_VERSION = "v4.0-preview"
PARALLEL_CAPACITY_SOURCE = "analytics_parallel_capacity"


def date_to_sk(value: date | datetime) -> int:
    """Convert a date/datetime to Azure DevOps Analytics SamplingDateSK (YYYYMMDD)."""
    return int(f"{value.year:04d}{value.month:02d}{value.day:02d}")


def task_agent_slot_minutes(rows: list[dict[str, Any]], interval_minutes: int = 10) -> float:
    """Return job-slot-minutes from TaskAgentRequestSnapshots concurrency rows.

    TaskAgentRequestSnapshots is a 10-minute snapshot table: a long-running job
    appears once per sampling interval. Summing row durations double-counts. The
    supported pattern is aggregate to the maximum running-job count per interval,
    then sum MaxCount * 10 minutes.
    """
    total = 0.0
    for row in rows:
        total += float(row.get("MaxCount") or row.get("Count") or 0) * interval_minutes
    return total


def _odata_url(org: str, project: str, entity: str, apply: str, orderby: str | None = None) -> str:
    query = "$apply=" + quote(apply, safe="(),/$=':-._")
    if orderby:
        query += "&$orderby=" + quote(orderby, safe=" ,")
    return f"https://analytics.dev.azure.com/{org}/{project}/_odata/{ODATA_VERSION}/{entity}?{query}"


def parallel_capacity_apply(start: datetime, end: datetime) -> str:
    """Return the ParallelPipelineJobsSnapshot parallel-job entitlement aggregation.

    ParallelPipelineJobsSnapshot describes the parallel-job *entitlement* in force at
    each sampling time, not consumed build minutes. ``TotalCount`` is the number of
    licensed parallel job slots for a ``ParallelismTag`` (for example 1 hosted private
    slot, or 100000 for effectively unlimited public slots) and ``TotalMinutes`` is the
    fixed monthly Microsoft-hosted free-minute grant (typically 1800). Both are
    constants that are re-sampled many times per day, so a ``sum`` multiplies a capacity
    constant by the number of snapshots taken. Aggregate with ``max`` instead to recover
    the entitlement that applied during the window.
    """
    start_s = start.date().isoformat()
    end_s = end.date().isoformat()
    return (
        f"filter(SamplingDate ge {start_s}Z and SamplingDate le {end_s}Z)"
        "/groupby((IsHosted,ParallelismTag),"
        "aggregate(TotalCount with max as MaxParallelJobs,TotalMinutes with max as MaxFreeMinutesGrant))"
    )


def task_agent_apply(start: datetime, end: datetime) -> str:
    """Return the safe TaskAgentRequestSnapshots OData aggregation."""
    return (
        f"filter(SamplingDateSK ge {date_to_sk(start)} and SamplingDateSK le {date_to_sk(end)} and IsRunning eq true)"
        "/groupby((SamplingDateSK,SamplingHour,SamplingTime,IsHosted,PoolId),aggregate($count as Count))"
        "/groupby((SamplingTime,IsHosted,PoolId),aggregate(Count with max as MaxCount))"
    )


async def _odata_rows(client: AzureDevOpsHttpClient, url: str, warnings: list[str], max_pages: int = 10_000) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    page_count = 0
    while url:
        if page_count >= max_pages:
            raise AdoHttpError(0, url, f"OData pagination exceeded max_pages={max_pages}")
        if url in seen_urls:
            raise AdoHttpError(0, url, "OData pagination made no forward progress; repeated nextLink")
        seen_urls.add(url)
        body, _ = await client.get_json(url)
        page_count += 1
        if isinstance(body, dict):
            for warning in body.get("@vsts.warnings", []) or []:
                warnings.append(str(warning))
            previous_url = url
            rows.extend(item for item in body.get("value", []) if isinstance(item, dict))
            url = body.get("@odata.nextLink") or ""
            if url == previous_url:
                raise AdoHttpError(0, url, "OData pagination made no forward progress; unchanged nextLink")
        else:
            url = ""
    return rows


async def _collect_parallel_capacity(
    client: AzureDevOpsHttpClient,
    org: str,
    project: str,
    start: datetime,
    end: datetime,
    warnings: list[str],
) -> list[UsageRecord]:
    """Collect parallel-job entitlement from ParallelPipelineJobsSnapshot.

    These records are capacity metadata only. They deliberately carry 0 minutes and 0
    jobs so they can never be mistaken for, or aggregated into, consumed build minutes.
    """
    apply = parallel_capacity_apply(start, end)
    rows = await _odata_rows(client, _odata_url(org, project, "ParallelPipelineJobsSnapshot", apply), warnings)
    records: list[UsageRecord] = []
    for row in rows:
        records.append(
            UsageRecord(
                source=PARALLEL_CAPACITY_SOURCE,
                org=org,
                project=project,
                runner_type=CAPACITY_NOT_MINUTES,
                minutes=0.0,
                jobs=0.0,
                parallelism_tag=row.get("ParallelismTag"),
                granularity="parallel_job_entitlement",
                extra={
                    "parallel_jobs_granted": row.get("MaxParallelJobs"),
                    "hosted_free_minutes_grant": row.get("MaxFreeMinutesGrant"),
                    "is_hosted": row.get("IsHosted"),
                },
            )
        )
    if rows:
        warnings.append(
            f"{org}/{project}: ParallelPipelineJobsSnapshot reports parallel-job entitlement (licensed slots and the "
            "monthly hosted free-minute grant), not consumed minutes; it contributes 0 minutes to headline totals."
        )
    return records


async def _collect_task_agent_snapshots(
    client: AzureDevOpsHttpClient,
    org: str,
    project: str,
    start: datetime,
    end: datetime,
    warnings: list[str],
    pools: dict[int, PoolInfo],
) -> list[UsageRecord]:
    apply = task_agent_apply(start, end)
    rows = await _odata_rows(client, _odata_url(org, project, "TaskAgentRequestSnapshots", apply, "SamplingTime asc"), warnings)
    records: list[UsageRecord] = []
    for row in rows:
        sampling_time = row.get("SamplingTime")
        max_count = float(row.get("MaxCount") or 0)
        minutes = max_count * 10.0
        pool_id = int(row["PoolId"]) if row.get("PoolId") is not None else None
        pool = pools.get(pool_id) if pool_id is not None else None
        if not pool:
            warnings.append(f"{org}/{project}: TaskAgentRequestSnapshots row has unknown PoolId {pool_id}; minutes are unclassified.")
        records.append(
            UsageRecord(
                source="analytics_taskagent_slots",
                org=org,
                project=project,
                runner_type=pool.runner_type if pool else UNKNOWN,
                pool_id=pool_id,
                pool_name=pool.name if pool else None,
                minutes=minutes,
                jobs=0,
                month=month_key(str(sampling_time) if sampling_time else start),
                started_at=str(sampling_time) if sampling_time else None,
                granularity="10_minute_slot_aggregate",
                extra={"max_concurrent_slots": max_count, "slot_intervals": 1},
            )
        )
    if rows:
        computed = task_agent_slot_minutes(rows)
        warnings.append(
            f"{org}/{project}: TaskAgentRequestSnapshots reported {computed:.1f} job-slot-minutes using the documented MaxCount × 10 aggregation."
        )
    return records


async def collect_analytics(
    client: AzureDevOpsHttpClient,
    orgs: list[str],
    start: datetime,
    end: datetime,
) -> CollectionResult:
    """Collect Analytics OData aggregate minutes from pipeline and pool snapshot entities."""
    result = CollectionResult()
    result.expected_orgs = list(orgs)
    for org in orgs:
        try:
            projects = await list_projects(client, org)
        except AdoHttpError as exc:
            result.failures.append(Failure("analytics", org, None, exc.status_code, exc.body or str(exc)))
            continue
        try:
            pools = await fetch_pools(client, org, enrich_agents=False)
        except AdoHttpError as exc:
            pools = {}
            result.failures.append(Failure("analytics_pools", org, None, exc.status_code, exc.body or str(exc)))

        async def collect_project(project_entry: dict[str, Any]) -> tuple[list[UsageRecord], list[str], list[Failure]]:
            project = str(project_entry.get("name") or project_entry.get("id"))
            records: list[UsageRecord] = []
            warnings: list[str] = []
            failures: list[Failure] = []
            try:
                records.extend(await _collect_parallel_capacity(client, org, project, start, end, warnings))
            except AdoHttpError as exc:
                failures.append(Failure(PARALLEL_CAPACITY_SOURCE, org, project, exc.status_code, exc.body or str(exc)))
            try:
                records.extend(await _collect_task_agent_snapshots(client, org, project, start, end, warnings, pools))
            except AdoHttpError as exc:
                message = exc.body or str(exc)
                if exc.status_code == 403:
                    message = (
                        "TaskAgentRequestSnapshots returned 403. Project Collection Administrator is required "
                        "for Azure DevOps pool-consumption snapshot entities. " + message
                    )
                failures.append(Failure("analytics_taskagent_slots", org, project, exc.status_code, message))
            return records, warnings, failures

        project_results = await asyncio.gather(*(collect_project(project_entry) for project_entry in projects))
        result.mark_source_covered(PARALLEL_CAPACITY_SOURCE, org)
        result.mark_source_covered("analytics_taskagent_slots", org)
        for records, warnings, failures in project_results:
            result.records.extend(records)
            result.warnings.extend(warnings)
            result.failures.extend(failures)
    return result
