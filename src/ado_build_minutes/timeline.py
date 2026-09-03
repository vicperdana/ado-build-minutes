"""Authoritative but expensive build timeline collector."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from .classify import UNKNOWN
from .http import AdoHttpError, AzureDevOpsHttpClient
from .models import CollectionResult, Failure, PoolInfo, UsageRecord, duration_minutes, month_key
from .pools import API_VERSION, fetch_pools, fetch_project_queues, list_projects


TIMELINE_MAX_PAGES = 10_000
TIMELINE_CHECKPOINT_BATCH_SIZE = 25


def dedupe_timeline_jobs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return unique timeline Job records keyed by (identifier, attempt)."""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for record in records:
        if record.get("type") != "Job":
            continue
        key = (str(record.get("identifier") or record.get("id") or ""), str(record.get("attempt") or "1"))
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


class TimelineCheckpoint:
    """JSON checkpoint store for resumable timeline runs, including durable records."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.data: dict[str, Any] = self._load()
        self._dirty_builds = 0

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        with self.path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def _entry(self, key: str) -> dict[str, Any]:
        return self.data.setdefault(key, {"completed_build_ids": [], "records": [], "continuation_token": None})

    def completed_builds(self, key: str) -> set[int]:
        """Return completed build ids that have persisted records in the checkpoint."""
        entry = self.data.get(key, {})
        if "records" not in entry:
            return set()
        return {int(item) for item in entry.get("completed_build_ids", [])}

    def records(self, key: str) -> list[UsageRecord]:
        """Return previously persisted timeline records for a checkpoint key."""
        records: list[UsageRecord] = []
        for payload in self.data.get(key, {}).get("records", []) or []:
            if isinstance(payload, dict):
                try:
                    records.append(UsageRecord(**payload))
                except TypeError:
                    continue
        return records

    def mark_build_records_completed(self, key: str, build_id: int, records: list[UsageRecord]) -> None:
        """Buffer records and completion state; callers flush batches durably."""
        entry = self._entry(key)
        ids = set(int(item) for item in entry.get("completed_build_ids", []))
        ids.add(int(build_id))
        entry["completed_build_ids"] = sorted(ids)
        entry.setdefault("records", []).extend(asdict(record) for record in records)
        self._dirty_builds += 1

    def flush_if_needed(self, force: bool = False) -> None:
        """Persist buffered records/completion state in batches."""
        if force or self._dirty_builds >= TIMELINE_CHECKPOINT_BATCH_SIZE:
            self.save()
            self._dirty_builds = 0

    def set_continuation(self, key: str, token: str | None, *, save: bool = True) -> None:
        """Save the latest build-list continuation token."""
        entry = self._entry(key)
        entry["continuation_token"] = token
        if save:
            self.save()
            self._dirty_builds = 0

    def continuation(self, key: str) -> str | None:
        """Return the last saved continuation token for a key, if any."""
        return self.data.get(key, {}).get("continuation_token")

    def save(self) -> None:
        """Persist the checkpoint without using temporary directories."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        next_path = self.path.with_name(self.path.name + ".new")
        with next_path.open("w", encoding="utf-8") as handle:
            json.dump(self.data, handle, indent=2, sort_keys=True)
        next_path.replace(self.path)


def _record_from_timeline_job(
    org: str,
    project: str,
    build: dict[str, Any],
    job: dict[str, Any],
    queues: dict[int, dict[str, Any]],
    pools: dict[int, PoolInfo],
    warnings: list[str] | None = None,
) -> UsageRecord | None:
    minutes = duration_minutes(job.get("startTime"), job.get("finishTime"))
    if minutes is None:
        return None
    queue_id = job.get("queueId")
    queue = queues.get(int(queue_id)) if queue_id is not None else None
    pool_payload = (queue or {}).get("pool") or {}
    pool_id = pool_payload.get("id")
    pool = pools.get(int(pool_id)) if pool_id is not None else None
    if pool:
        runner_type = pool.runner_type
        pool_name = pool.name
    else:
        runner_type = UNKNOWN
        pool_name = pool_payload.get("name")
        if warnings is not None:
            warnings.append(
                f"{org}/{project}/build {build.get('id')}: timeline job {job.get('identifier') or job.get('id')} "
                f"references unknown/deleted queueId={queue_id} poolId={pool_id}; minutes are unclassified."
            )
    definition = build.get("definition") or {}
    return UsageRecord(
        source="timeline",
        org=org,
        project=project,
        pipeline=definition.get("name"),
        runner_type=runner_type,
        pool_id=int(pool_id) if pool_id is not None else None,
        pool_name=pool_name,
        minutes=minutes,
        jobs=1,
        month=month_key(job.get("finishTime") or build.get("finishTime")),
        started_at=job.get("startTime"),
        finished_at=job.get("finishTime"),
        result=job.get("result"),
        build_id=build.get("id"),
        definition=definition.get("name"),
        granularity="job",
        extra={"timeline_identifier": job.get("identifier"), "timeline_attempt": job.get("attempt"), "queue_id": queue_id},
    )


async def _fetch_build_timeline_records(
    client: AzureDevOpsHttpClient,
    org: str,
    project: str,
    build: dict[str, Any],
    queues: dict[int, dict[str, Any]],
    pools: dict[int, PoolInfo],
) -> tuple[int, list[UsageRecord], list[str], Failure | None]:
    build_id = int(build.get("id"))
    timeline_url = f"https://dev.azure.com/{org}/{project}/_apis/build/builds/{build_id}/timeline"
    try:
        timeline_body, _ = await client.get_json(timeline_url, params={"api-version": API_VERSION})
    except AdoHttpError as exc:
        return build_id, [], [], Failure("timeline_record", org, project, exc.status_code, f"build {build_id}: {exc.body or str(exc)}")
    jobs = dedupe_timeline_jobs(timeline_body.get("records", []) if isinstance(timeline_body, dict) else [])
    records: list[UsageRecord] = []
    warnings: list[str] = []
    for job in jobs:
        record = _record_from_timeline_job(org, project, build, job, queues, pools, warnings)
        if record:
            records.append(record)
    return build_id, records, warnings, None


async def collect_timeline(
    client: AzureDevOpsHttpClient,
    orgs: list[str],
    start: datetime,
    end: datetime,
    state_file: str,
) -> CollectionResult:
    """Collect authoritative per-job minutes from builds and timeline records."""
    result = CollectionResult(expected_orgs=list(orgs))
    checkpoint = TimelineCheckpoint(state_file)
    for org in orgs:
        try:
            pools = await fetch_pools(client, org, enrich_agents=True)
            projects = await list_projects(client, org)
        except AdoHttpError as exc:
            result.failures.append(Failure("timeline", org, None, exc.status_code, exc.body or str(exc)))
            continue
        result.mark_source_covered("timeline", org)
        for project_entry in projects:
            project = str(project_entry.get("name") or project_entry.get("id"))
            key = f"timeline:{org}:{project}:{start.date()}:{end.date()}"
            result.records.extend(checkpoint.records(key))
            completed = checkpoint.completed_builds(key)
            try:
                queues = await fetch_project_queues(client, org, project)
            except AdoHttpError as exc:
                result.failures.append(Failure("timeline_queues", org, project, exc.status_code, exc.body or str(exc)))
                continue
            url = f"https://dev.azure.com/{org}/{project}/_apis/build/builds"
            params = {
                "api-version": API_VERSION,
                "minTime": start.isoformat().replace("+00:00", "Z"),
                "maxTime": end.isoformat().replace("+00:00", "Z"),
                "statusFilter": "completed",
                "queryOrder": "finishTimeAscending",
                "$top": "500",
            }
            continuation = checkpoint.continuation(key)
            seen_tokens: set[str] = set()
            page_count = 0
            while True:
                if page_count >= TIMELINE_MAX_PAGES:
                    result.failures.append(Failure("timeline_builds", org, project, 0, f"Pagination exceeded {TIMELINE_MAX_PAGES} pages"))
                    break
                if continuation:
                    if continuation in seen_tokens:
                        result.failures.append(Failure("timeline_builds", org, project, 0, f"Repeated continuation token {continuation!r}"))
                        break
                    seen_tokens.add(continuation)
                try:
                    body, headers = await client.get_json(url, params=params | ({"continuationToken": continuation} if continuation else {}))
                except AdoHttpError as exc:
                    result.failures.append(Failure("timeline_builds", org, project, exc.status_code, exc.body or str(exc)))
                    break
                page_count += 1
                builds = body.get("value", []) if isinstance(body, dict) else []
                next_token = headers.get("x-ms-continuationtoken") or headers.get("X-MS-ContinuationToken")
                pending_builds = [build for build in builds if int(build.get("id")) not in completed]
                page_results = await asyncio.gather(
                    *(_fetch_build_timeline_records(client, org, project, build, queues, pools) for build in pending_builds)
                )
                for build_id, records, warnings, failure in page_results:
                    if failure:
                        result.failures.append(failure)
                        continue
                    result.records.extend(records)
                    result.warnings.extend(warnings)
                    checkpoint.mark_build_records_completed(key, build_id, records)
                    completed.add(build_id)
                    checkpoint.flush_if_needed()
                checkpoint.set_continuation(key, next_token, save=False)
                checkpoint.flush_if_needed(force=True)
                if not next_token:
                    checkpoint.set_continuation(key, None)
                    break
                if next_token == continuation:
                    result.failures.append(Failure("timeline_builds", org, project, 0, f"Unchanged continuation token {next_token!r}"))
                    break
                continuation = next_token
    return result
