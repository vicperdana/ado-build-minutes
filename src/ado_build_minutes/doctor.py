"""Preflight checks for Azure DevOps org permissions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover - fallback is for dependency-free help/test environments.
    Console = object  # type: ignore[assignment]

    class Table:  # type: ignore[no-redef]
        def __init__(self, title: str = "") -> None:
            self.title = title
            self.columns: list[str] = []
            self.rows: list[tuple[str, ...]] = []

        def add_column(self, column: str) -> None:
            self.columns.append(column)

        def add_row(self, *values: str) -> None:
            self.rows.append(tuple(values))

        def __str__(self) -> str:
            lines = [self.title, " | ".join(self.columns)]
            lines.extend(" | ".join(row) for row in self.rows)
            return "\n".join(lines)

from .auth import AuthProvider
from .analytics import ODATA_VERSION, parallel_snapshot_apply, task_agent_apply
from .http import AdoHttpError, AzureDevOpsHttpClient
from .pools import API_VERSION, list_projects


@dataclass
class DoctorRow:
    """One org's source-aware preflight status."""

    org: str
    token: str = "unknown"
    org_reachability: str = "unknown"
    projects: str = "unknown"
    pools: str = "unknown"
    analytics_parallel: str = "not requested"
    analytics_taskagent: str = "not requested"
    jobrequests: str = "not requested"
    timeline: str = "not requested"
    billing: str = "not requested"
    notes: str = ""


def _status(ok: bool, detail: str | None = None) -> str:
    return "PASS" + (f": {detail}" if ok and detail else "") if ok else f"FAIL{': ' + detail if detail else ''}"


def _sample_projects(projects: list[dict[str, Any]], limit: int = 3) -> list[str]:
    return [str(project.get("name") or project.get("id")) for project in projects[:limit]]


def _odata_probe_url(org: str, project: str, entity: str, apply: str, orderby: str | None = None) -> str:
    query = "$apply=" + quote(apply, safe="(),/$=':-._")
    if orderby:
        query += "&$orderby=" + quote(orderby, safe=" ,")
    return f"https://analytics.dev.azure.com/{org}/{project}/_odata/{ODATA_VERSION}/{entity}?{query}"


async def _probe_get(client: AzureDevOpsHttpClient, url: str, params: dict[str, Any] | None = None) -> str:
    try:
        await client.get_json(url, params=params)
        return "PASS"
    except AdoHttpError as exc:
        detail = f"HTTP {exc.status_code}"
        if exc.status_code == 403:
            detail += " permission denied"
        return _status(False, detail)


async def run_doctor(
    client: AzureDevOpsHttpClient,
    auth: AuthProvider,
    orgs: list[str],
    console: Console,
    sources: list[str] | None = None,
) -> list[DoctorRow]:
    """Verify token acquisition and the endpoints needed by requested sources."""
    requested = set(sources or ["analytics"])
    rows: list[DoctorRow] = []
    try:
        auth.authorization_header(force_refresh=True)
        token_ok = True
        token_note = ""
    except Exception as exc:  # noqa: BLE001 - doctor reports any credential issue clearly.
        token_ok = False
        token_note = str(exc)
    for org in orgs:
        row = DoctorRow(org=org, token=_status(token_ok, token_note if not token_ok else None))
        if not token_ok:
            rows.append(row)
            continue
        try:
            await client.get_json(f"https://dev.azure.com/{org}/_apis/connectionData", params={"api-version": API_VERSION})
            row.org_reachability = "PASS"
        except AdoHttpError as exc:
            row.org_reachability = _status(False, f"HTTP {exc.status_code}")
            row.notes += (exc.body or str(exc))[:180]
        projects: list[dict[str, Any]] = []
        try:
            projects = await list_projects(client, org)
            project_detail = f"{len(projects)} projects"
            if len(projects) > 3:
                project_detail += "; endpoint probes sample first 3 only"
            row.projects = _status(True, project_detail)
        except AdoHttpError as exc:
            row.projects = _status(False, f"HTTP {exc.status_code}")
            row.notes += " projects: " + (exc.body or str(exc))[:180]
        pools: list[dict[str, Any]] = []
        try:
            body, _ = await client.get_json(f"https://dev.azure.com/{org}/_apis/distributedtask/pools", params={"api-version": API_VERSION})
            pools = body.get("value", []) if isinstance(body, dict) else []
            pool_detail = f"{len(pools)} pools"
            if len(pools) > 3:
                pool_detail += "; jobrequest probes sample first 3 only"
            row.pools = _status(True, pool_detail)
        except AdoHttpError as exc:
            row.pools = _status(False, f"HTTP {exc.status_code}")
            row.notes += " pools: " + (exc.body or str(exc))[:180]

        sample_projects = _sample_projects(projects)
        if "analytics" in requested:
            if sample_projects:
                parallel_statuses = []
                taskagent_statuses = []
                end = datetime.now(timezone.utc)
                start = end - timedelta(days=1)
                for project_name in sample_projects:
                    parallel_url = _odata_probe_url(org, project_name, "ParallelPipelineJobsSnapshot", parallel_snapshot_apply(start, end))
                    taskagent_url = _odata_probe_url(org, project_name, "TaskAgentRequestSnapshots", task_agent_apply(start, end), "SamplingTime asc")
                    parallel_statuses.append(await _probe_get(client, parallel_url))
                    taskagent_statuses.append(await _probe_get(client, taskagent_url))
                row.analytics_parallel = "PASS" if all(status == "PASS" for status in parallel_statuses) else "; ".join(parallel_statuses)
                row.analytics_taskagent = "PASS" if all(status == "PASS" for status in taskagent_statuses) else "; ".join(taskagent_statuses)
                if len(projects) > len(sample_projects):
                    row.notes += " analytics sampled, not full project coverage."
            else:
                row.analytics_parallel = "SKIP: no project visible"
                row.analytics_taskagent = "SKIP: no project visible"
        if "jobrequests" in requested:
            if pools:
                statuses = []
                for pool in pools[:3]:
                    pool_id = int(pool["id"])
                    url = f"https://dev.azure.com/{org}/_apis/distributedtask/pools/{pool_id}/jobrequests"
                    statuses.append(await _probe_get(client, url, {"api-version": API_VERSION, "completedRequestCount": "1"}))
                row.jobrequests = "PASS" if all(status == "PASS" for status in statuses) else "; ".join(statuses)
            else:
                row.jobrequests = "SKIP: no pools visible"
        if "timeline" in requested:
            if sample_projects:
                statuses = []
                for project_name in sample_projects:
                    url = f"https://dev.azure.com/{org}/{project_name}/_apis/build/builds"
                    statuses.append(await _probe_get(client, url, {"api-version": API_VERSION, "$top": "1", "statusFilter": "completed"}))
                    queues_url = f"https://dev.azure.com/{org}/{project_name}/_apis/distributedtask/queues"
                    statuses.append(await _probe_get(client, queues_url, {"api-version": API_VERSION}))
                row.timeline = "PASS" if all(status == "PASS" for status in statuses) else "; ".join(statuses)
            else:
                row.timeline = "SKIP: no project visible"
        if "billing" in requested:
            billing_statuses = []
            try:
                payload = {"contributionIds": ["ms.vss-build-web.build-queue-hub-data-provider"], "dataProviderContext": {"properties": {}}}
                await client.post_json(f"https://dev.azure.com/{org}/_apis/Contribution/dataProviders/query", payload=payload)
            except AdoHttpError as exc:
                billing_statuses.append(_status(False, f"data-provider HTTP {exc.status_code}"))
            else:
                billing_statuses.append("PASS")
            resource_status = await _probe_get(
                client,
                f"https://dev.azure.com/{org}/_apis/build/resourceusage",
                {"api-version": "7.1-preview.2"},
            )
            billing_statuses.append(resource_status if resource_status != "PASS" else "PASS")
            row.billing = "PASS" if all(status == "PASS" for status in billing_statuses) else "; ".join(billing_statuses)
        rows.append(row)
    print_doctor_table(rows, console)
    return rows


def print_doctor_table(rows: list[DoctorRow], console: Console) -> None:
    """Render doctor results as a clear pass/fail table."""
    table = Table(title="Azure DevOps access preflight")
    for column in ("Org", "Token", "Org reachable", "Projects", "Pools", "Analytics", "TaskAgent", "JobRequests", "Timeline", "Billing", "Notes"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            row.org,
            row.token,
            row.org_reachability,
            row.projects,
            row.pools,
            row.analytics_parallel,
            row.analytics_taskagent,
            row.jobrequests,
            row.timeline,
            row.billing,
            row.notes[:240],
        )
    console.print(table)
