"""CSV, JSON, and Markdown output generation."""

from __future__ import annotations

from collections import defaultdict
import csv
import json
from pathlib import Path
from typing import Any, Iterable

from .classify import GITHUB_HOSTED, MICROSOFT_HOSTED, UNKNOWN
from .models import CollectionResult, Failure, UsageRecord

HEADLINE_SOURCE_ORDER = ["timeline", "analytics_taskagent_slots"]
EXPLICIT_HEADLINE_SOURCE_MAP = {
    "analytics": "analytics_taskagent_slots",
    "analytics_taskagent_slots": "analytics_taskagent_slots",
    "timeline": "timeline",
    "jobrequests": "jobrequests",
}
SOURCE_ROLES = {
    "analytics_parallel_capacity": "parallel-job entitlement and hosted free-minute grant; capacity, never minutes",
    "analytics_taskagent_slots": "default headline pool-classified job-slot-minutes (MaxCount x 10)",
    "jobrequests": "recent enrichment and reconciliation; headline only when explicitly selected alone",
    "timeline": "authoritative headline only when coverage is complete",
    "billing_unsupported": "current-period cross-check only",
    "billing_resourceusage_capacity": "capacity snapshot, not minutes",
}
FAILURE_SOURCE_GROUPS = {
    "analytics_parallel_capacity": {"analytics", "analytics_parallel_capacity"},
    "analytics_taskagent_slots": {"analytics", "analytics_taskagent_slots"},
    "timeline": {"timeline", "timeline_builds", "timeline_queues", "timeline_record"},
    "jobrequests": {"jobrequests"},
}
# Sources that describe licensed capacity rather than consumed minutes. They must never
# be selected as a headline source, and their records always carry 0 minutes and 0 jobs.
CAPACITY_ONLY_SOURCES = {"analytics_parallel_capacity", "billing_resourceusage_capacity"}
HOSTED_RUNNER_TYPES = {MICROSOFT_HOSTED, GITHUB_HOSTED}


def _record_dicts(records: Iterable[UsageRecord]) -> list[dict[str, Any]]:
    return [record.as_dict() for record in records]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)


def aggregate(records: list[UsageRecord], keys: list[str], source_filter: str | None = None) -> list[dict[str, Any]]:
    """Aggregate usage records by selected UsageRecord field names."""
    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        if source_filter and record.source != source_filter:
            continue
        key = tuple(getattr(record, item) for item in keys)
        bucket = buckets.setdefault(
            key,
            {item: value for item, value in zip(keys, key, strict=True)}
            | {"minutes": 0.0, "hours": 0.0, "jobs": 0.0, "sources": set()},
        )
        bucket["minutes"] += record.minutes
        bucket["jobs"] += record.jobs
        bucket["sources"].add(record.source)
    rows = []
    for bucket in buckets.values():
        bucket["minutes"] = round(bucket["minutes"], 4)
        bucket["hours"] = round(bucket["minutes"] / 60.0, 4)
        bucket["jobs"] = round(bucket["jobs"], 4)
        bucket["sources"] = ",".join(sorted(bucket["sources"]))
        rows.append(bucket)
    return sorted(rows, key=lambda row: tuple(str(row.get(key) or "") for key in keys))


def _source_failure_sources(source: str) -> set[str]:
    return FAILURE_SOURCE_GROUPS.get(source, {source})


def _expected_orgs(records: list[UsageRecord], expected_orgs: list[str] | None, failures: list[Failure] | None) -> list[str]:
    orgs = list(dict.fromkeys(expected_orgs or []))
    if not orgs:
        for org in [record.org for record in records] + [failure.org for failure in (failures or [])]:
            if org not in orgs:
                orgs.append(org)
    return orgs


def source_coverage(
    records: list[UsageRecord],
    source: str,
    expected_orgs: list[str] | None = None,
    failures: list[Failure] | None = None,
    covered_orgs_by_source: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Summarise per-org coverage for a source."""
    expected = _expected_orgs(records, expected_orgs, failures)
    covered = sorted({record.org for record in records if record.source == source} | set((covered_orgs_by_source or {}).get(source, [])))
    missing = [org for org in expected if org not in covered]
    failure_sources = _source_failure_sources(source)
    related_failures = [failure for failure in (failures or []) if failure.source in failure_sources]
    complete = bool(covered) and not missing and not related_failures
    return {
        "source": source,
        "role": SOURCE_ROLES.get(source, "additional source"),
        "covered_orgs": covered,
        "missing_orgs": missing,
        "failure_count": len(related_failures),
        "complete": complete,
    }


def choose_primary_source(
    records: list[UsageRecord],
    expected_orgs: list[str] | None = None,
    failures: list[Failure] | None = None,
    explicit_headline_source: str | None = None,
    covered_orgs_by_source: dict[str, list[str]] | None = None,
) -> str | None:
    """Choose a complete headline source without double-counting or hiding partial coverage."""
    available = {record.source for record in records}
    candidates: list[str] = []
    explicit = EXPLICIT_HEADLINE_SOURCE_MAP.get(explicit_headline_source or "")
    if explicit:
        candidates.append(explicit)
    for source in HEADLINE_SOURCE_ORDER:
        if source not in candidates:
            candidates.append(source)
    for source in candidates:
        if source in CAPACITY_ONLY_SOURCES:
            continue
        if (source in available or source in (covered_orgs_by_source or {})) and source_coverage(
            records, source, expected_orgs, failures, covered_orgs_by_source
        ).get("complete"):
            return source
    return None


def _source_totals(records: list[UsageRecord]) -> list[dict[str, Any]]:
    return aggregate(records, ["source", "runner_type"])


def _hosted_minutes(records: list[UsageRecord], source: str) -> float:
    return sum(record.minutes for record in records if record.source == source and record.runner_type in HOSTED_RUNNER_TYPES)


def infer_actions_rate_key(record: UsageRecord) -> str | None:
    """Infer an editable cost-rate key from known image/OS/pool information."""
    text = " ".join(str(part or "") for part in (record.image, record.pool_name, record.runner_type)).casefold()
    if "mac" in text or "osx" in text:
        return "macos_per_minute"
    if "windows" in text or "win" in text:
        return "windows_per_minute"
    if "ubuntu" in text or "linux" in text:
        return "linux_per_minute"
    if record.runner_type in HOSTED_RUNNER_TYPES:
        return "unknown_hosted_os"
    if record.runner_type in {"self_hosted", "vmss_elastic_pool", "managed_devops_pool"}:
        return "self_hosted_per_minute"
    return None


def actions_cost_rows(records: list[UsageRecord], rates: dict[str, float], primary_source: str | None) -> tuple[list[dict[str, Any]], list[str]]:
    """Estimate GitHub Actions costs using user-supplied editable config rates."""
    warnings: list[str] = []
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    if not primary_source:
        return [], warnings
    for record in records:
        if record.source != primary_source:
            continue
        rate_key = infer_actions_rate_key(record)
        if rate_key == "unknown_hosted_os":
            key = (record.runner_type, rate_key)
            row = rows.setdefault(
                key,
                {"runner_type": record.runner_type, "rate_key": rate_key, "minutes": 0.0, "rate": "unpriced", "estimated_cost": "unpriced"},
            )
            row["minutes"] += record.minutes
            warnings.append(
                f"Hosted minutes for {record.org}/{record.project or 'unknown project'} have no image/OS; placed in unknown_hosted_os and left unpriced."
            )
            continue
        if not rate_key or rate_key not in rates:
            warnings.append(f"No Actions cost rate configured for {record.runner_type}/{record.image or record.pool_name or 'unknown'}; estimate omitted.")
            continue
        key = (record.runner_type, rate_key)
        row = rows.setdefault(key, {"runner_type": record.runner_type, "rate_key": rate_key, "minutes": 0.0, "rate": rates[rate_key], "estimated_cost": 0.0})
        row["minutes"] += record.minutes
        row["estimated_cost"] += record.minutes * rates[rate_key]
    for row in rows.values():
        row["minutes"] = round(row["minutes"], 4)
        if isinstance(row["estimated_cost"], float):
            row["estimated_cost"] = round(row["estimated_cost"], 4)
    return sorted(rows.values(), key=lambda row: (row["runner_type"], row["rate_key"])), warnings


def build_summaries(records: list[UsageRecord], primary_source: str | None) -> dict[str, list[dict[str, Any]]]:
    """Build all required summary tables."""
    primary_records = [record for record in records if primary_source and record.source == primary_source]
    return {
        "by_source_runner_type": _source_totals(records),
        "by_org": aggregate(primary_records, ["org"]),
        "by_org_runner_type": aggregate(primary_records, ["org", "runner_type"]),
        "by_org_pool": aggregate(primary_records, ["org", "pool_id", "pool_name"]),
        "by_org_runner_type_image": aggregate(primary_records, ["org", "runner_type", "image"]),
        "by_month_runner_type": aggregate(primary_records, ["month", "runner_type"]),
    }


def _markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_No rows._\n"
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join("---" if not col.endswith(("minutes", "hours", "jobs")) else "---:" for col in columns) + " |"
    lines = [header, sep]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "")) for col in columns) + " |")
    return "\n".join(lines) + "\n"


def _provenance_rows(
    records: list[UsageRecord],
    expected_orgs: list[str] | None,
    failures: list[Failure],
    covered_orgs_by_source: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    expected = _expected_orgs(records, expected_orgs, failures)
    source_map: dict[str, set[str]] = defaultdict(set)
    for record in records:
        source_map[record.org].add(record.source)
    for source, orgs in (covered_orgs_by_source or {}).items():
        for org in orgs:
            source_map[org].add(source)
    return [
        {"org": org, "sources": ",".join(sorted(source_map.get(org, set()))) or "missing", "missing_entirely": "yes" if not source_map.get(org) else "no"}
        for org in expected
    ]


def _needs_provenance_warning(
    records: list[UsageRecord],
    primary_source: str | None,
    expected_orgs: list[str] | None,
    failures: list[Failure],
    covered_orgs_by_source: dict[str, list[str]] | None = None,
) -> bool:
    if not records:
        return False
    if primary_source is None:
        return True
    coverage = source_coverage(records, primary_source, expected_orgs, failures, covered_orgs_by_source)
    return not bool(coverage.get("complete"))


def _billing_periods_align(records: list[UsageRecord], start: str, end: str) -> bool:
    billing = [record for record in records if record.source == "billing_unsupported"]
    if not billing:
        return False
    for record in billing:
        if record.extra.get("period_start") != start or record.extra.get("period_end") != end:
            return False
    return True


def write_markdown_summary(
    path: Path,
    records: list[UsageRecord],
    summaries: dict[str, list[dict[str, Any]]],
    result: CollectionResult,
    primary_source: str | None,
    start: str,
    end: str,
    actions_rows: list[dict[str, Any]],
    expected_orgs: list[str] | None = None,
) -> None:
    """Write the customer-facing executive Markdown summary."""
    primary_hosted = _hosted_minutes(records, primary_source) if primary_source else 0.0
    billing_hosted = _hosted_minutes(records, "billing_unsupported")
    comparable_periods = _billing_periods_align(records, start, end)
    variance = primary_hosted - billing_hosted if primary_hosted and billing_hosted and comparable_periods else None
    variance_pct = (variance / billing_hosted * 100.0) if variance is not None and billing_hosted else None
    primary_totals = aggregate([record for record in records if primary_source and record.source == primary_source], ["runner_type"])
    top_pools = sorted(summaries["by_org_pool"], key=lambda row: row.get("minutes", 0), reverse=True)[:15]
    source_role_rows = [{"source": source, "role": role} for source, role in SOURCE_ROLES.items()]
    lines = [
        "# Azure DevOps build minutes summary",
        "",
        f"Reporting window: {start} to {end}",
        f"Primary source for headline totals: {primary_source or 'none - incomplete or mixed coverage'}",
        "",
        "Azure DevOps bills by parallel-job capacity rather than historical minutes, so these figures are reconstructed from Azure DevOps APIs. Do not add multiple sources together; use reconciliation to understand variance.",
        "",
    ]
    if _needs_provenance_warning(records, primary_source, expected_orgs or result.expected_orgs, result.failures, result.source_covered_orgs):
        lines.extend([
            "## ⚠️ MIXED PROVENANCE / INCOMPLETE COVERAGE",
            "",
            "No complete single headline source covered every requested organisation without source-level failures. A clean combined total is intentionally not shown.",
            "",
            _markdown_table(
                _provenance_rows(records, expected_orgs or result.expected_orgs, result.failures, result.source_covered_orgs),
                ["org", "sources", "missing_entirely"],
            ),
        ])
    if primary_source == "analytics_taskagent_slots":
        lines.extend([
            "## Headline job-slot-minutes by runner type",
            "",
            "`analytics_taskagent_slots` is the default headline source. It reports pool-classified job-slot-minutes "
            "aggregated as `MaxCount x 10`, which measures occupied concurrency rather than unique jobs, so the job "
            "count is reported as 0. Use `timeline` for authoritative per-build minutes where coverage allows.",
            "",
        ])
    else:
        lines.extend(["## Total minutes/hours by runner type", ""])
    lines.extend([
        _markdown_table(primary_totals, ["runner_type", "minutes", "hours", "jobs"]),
        "## Per-org summary",
        "",
        _markdown_table(summaries["by_org"], ["org", "minutes", "hours", "jobs", "sources"]),
        "## Per-org by runner type",
        "",
        _markdown_table(summaries["by_org_runner_type"], ["org", "runner_type", "minutes", "hours", "jobs", "sources"]),
        "## Top pools/pipelines available from selected source",
        "",
        _markdown_table(top_pools, ["org", "pool_id", "pool_name", "minutes", "hours", "jobs", "sources"]),
        "## Source roles",
        "",
        _markdown_table(source_role_rows, ["source", "role"]),
        "## Source provenance",
        "",
        _markdown_table(summaries["by_source_runner_type"], ["source", "runner_type", "minutes", "hours", "jobs"]),
        "## Reconciliation",
        "",
    ])
    if primary_hosted and billing_hosted and comparable_periods:
        lines.extend([
            f"- Hosted minutes from headline source (`{primary_source}`): {primary_hosted:.1f}",
            f"- Billing hosted minutes (`billing_unsupported`): {billing_hosted:.1f}",
            f"- Variance: {variance:.1f} minutes ({variance_pct:.2f}%).",
        ])
    elif primary_hosted and billing_hosted:
        lines.extend([
            f"- Hosted minutes from headline source for requested range (`{primary_source}`, {start} to {end}): {primary_hosted:.1f}",
            f"- Billing hosted minutes (`billing_unsupported`, current billing period): {billing_hosted:.1f}",
            "- These periods are not directly comparable, so no variance percentage is calculated.",
        ])
    else:
        lines.append("Headline-vs-billing reconciliation was not available because one or both sources were not selected or returned no hosted minutes.")
    if actions_rows:
        lines.extend([
            "",
            "## Optional GitHub Actions cost estimate",
            "",
            "Rates are user-supplied estimates — verify against current GitHub billing docs before quoting. Hosted minutes without known image/OS are reported as `unknown_hosted_os` and left unpriced.",
            "",
            _markdown_table(actions_rows, ["runner_type", "rate_key", "minutes", "rate", "estimated_cost"]),
        ])
    lines.extend([
        "",
        "## Caveats and limitations",
        "",
        "- Azure DevOps has no native cross-org build-minute report because parallel jobs, not minutes, are the paid unit.",
        "- `ParallelPipelineJobsSnapshot` (`analytics_parallel_capacity`) reports licensed parallel-job slots and the fixed monthly hosted free-minute grant. Those values are capacity constants re-sampled many times per day, so they are never summed into minutes.",
        "- `TaskAgentRequestSnapshots` must be aggregated as MaxCount × 10 minutes; naive duration sums double-count long jobs, and `MaxCount` is a max concurrent slot count, not a unique job count.",
        "- `jobrequests` is semi-documented/unsupported, has no server-side time filter, may be capped, and usually exposes only a recent retention window.",
        "- `billing_unsupported` is a UI data-provider endpoint for the current billing period only and may break without notice.",
        "- `timeline` is authoritative but expensive because there is one timeline request per build.",
        "- Hosted image breakdown is available at scale only from `jobrequests`; hosted minutes with unknown OS are unpriced in the Actions estimate.",
        "- Unknown/deleted queues are reported as `unknown`; they are never inferred to be self-hosted.",
        "- `build.queue.pool.isHosted` is deprecated and intentionally not used for job-level classification.",
        "",
        "## Warnings",
        "",
    ])
    lines.extend(f"- {warning}" for warning in sorted(set(result.warnings))) if result.warnings else lines.append("_None._")
    lines.extend(["", "## Failures", ""])
    if result.failures:
        failure_rows = [failure.__dict__ for failure in result.failures]
        lines.append(_markdown_table(failure_rows, ["source", "org", "project", "status_code", "message"]))
    else:
        lines.append("_None._")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_outputs(
    result: CollectionResult,
    out_dir: str,
    fmt: str,
    start: str,
    end: str,
    rates: dict[str, float] | None = None,
    include_actions_cost: bool = False,
    expected_orgs: list[str] | None = None,
    explicit_headline_source: str | None = None,
) -> dict[str, Any]:
    """Write requested outputs and return paths plus in-memory summaries."""
    out = Path(out_dir)
    records = result.records
    expected = expected_orgs or result.expected_orgs
    primary_source = choose_primary_source(records, expected, result.failures, explicit_headline_source, result.source_covered_orgs)
    summaries = build_summaries(records, primary_source)
    action_rows: list[dict[str, Any]] = []
    if include_actions_cost:
        action_rows, action_warnings = actions_cost_rows(records, rates or {}, primary_source)
        result.warnings.extend(action_warnings)
        if not rates:
            result.warnings.append("--actions-cost-model was requested but no [actions_cost_rates] were loaded from config.")
    unclassified = sum(record.minutes for record in records if record.runner_type == UNKNOWN)
    if unclassified:
        result.warnings.append(f"Unclassified minutes: {unclassified:.1f} from unknown/deleted queues or missing pool metadata.")
    payload = {
        "primary_source": primary_source,
        "records": _record_dicts(records),
        "summaries": summaries,
        "source_coverage": [
            source_coverage(records, source, expected, result.failures, result.source_covered_orgs)
            for source in sorted({record.source for record in records} | set(result.source_covered_orgs))
        ],
        "warnings": sorted(set(result.warnings)),
        "failures": [failure.__dict__ for failure in result.failures],
        "actions_cost_estimate": action_rows,
    }
    written: list[str] = []
    if fmt in {"csv", "all"}:
        detail_rows = _record_dicts(records)
        _write_csv(out / "detail.csv", detail_rows)
        written.append(str(out / "detail.csv"))
        for name, rows in summaries.items():
            file_name = {
                "by_org": "summary_by_org.csv",
                "by_org_runner_type": "summary_by_org_runner_type.csv",
                "by_org_pool": "summary_by_org_pool.csv",
                "by_org_runner_type_image": "summary_by_org_runner_type_image.csv",
                "by_month_runner_type": "summary_by_month_runner_type.csv",
                "by_source_runner_type": "summary_by_source_runner_type.csv",
            }[name]
            _write_csv(out / file_name, rows)
            written.append(str(out / file_name))
        if action_rows:
            _write_csv(out / "actions_cost_estimate.csv", action_rows)
            written.append(str(out / "actions_cost_estimate.csv"))
    if fmt in {"json", "all"}:
        _write_json(out / "detail.json", payload["records"])
        _write_json(out / "summary.json", payload)
        written.extend([str(out / "detail.json"), str(out / "summary.json")])
    if fmt in {"markdown", "all"}:
        write_markdown_summary(out / "executive_summary.md", records, summaries, result, primary_source, start, end, action_rows, expected)
        written.append(str(out / "executive_summary.md"))
    return {"written": written, "primary_source": primary_source, "summary": payload}
