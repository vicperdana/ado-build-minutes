"""Shared typed models for Azure DevOps build-minute collection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any


@dataclass(frozen=True)
class PoolInfo:
    """Normalized Azure DevOps agent pool metadata."""

    org: str
    id: int
    name: str
    runner_type: str
    is_hosted: bool | None = None
    pool_type: str | None = None
    options: Any = None
    agent_cloud_id: str | None = None
    size: int | None = None
    os_names: tuple[str, ...] = ()
    os_descriptions: tuple[str, ...] = ()


@dataclass
class UsageRecord:
    """A normalized job or aggregate usage record."""

    source: str
    org: str
    runner_type: str
    minutes: float
    jobs: float = 1.0
    project: str | None = None
    pipeline: str | None = None
    pool_id: int | None = None
    pool_name: str | None = None
    image: str | None = None
    month: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    queue_minutes: float | None = None
    result: str | None = None
    request_id: str | int | None = None
    build_id: str | int | None = None
    definition: str | None = None
    owner: str | None = None
    plan_type: str | None = None
    parallelism_tag: str | None = None
    granularity: str = "job"
    unsupported: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """Return a CSV/JSON friendly dictionary."""
        data = {
            "source": self.source,
            "org": self.org,
            "project": self.project,
            "pipeline": self.pipeline,
            "runner_type": self.runner_type,
            "pool_id": self.pool_id,
            "pool_name": self.pool_name,
            "image": self.image,
            "month": self.month,
            "minutes": round(self.minutes, 4),
            "hours": round(self.minutes / 60, 4),
            "jobs": self.jobs,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "queue_minutes": None if self.queue_minutes is None else round(self.queue_minutes, 4),
            "result": self.result,
            "request_id": self.request_id,
            "build_id": self.build_id,
            "definition": self.definition,
            "owner": self.owner,
            "plan_type": self.plan_type,
            "parallelism_tag": self.parallelism_tag,
            "granularity": self.granularity,
            "unsupported": self.unsupported,
        }
        for key, value in sorted(self.extra.items()):
            data[f"extra_{key}"] = value
        return data


@dataclass
class Failure:
    """A recoverable collection failure for one org/project/source."""

    source: str
    org: str
    project: str | None
    status_code: int | None
    message: str


@dataclass
class CollectionResult:
    """Records, warnings, and failures returned by collectors."""

    records: list[UsageRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    expected_orgs: list[str] = field(default_factory=list)
    source_covered_orgs: dict[str, list[str]] = field(default_factory=dict)

    def extend(self, other: "CollectionResult") -> None:
        """Merge another result into this one."""
        self.records.extend(other.records)
        self.warnings.extend(other.warnings)
        self.failures.extend(other.failures)
        for org in other.expected_orgs:
            if org not in self.expected_orgs:
                self.expected_orgs.append(org)
        for source, orgs in other.source_covered_orgs.items():
            for org in orgs:
                self.mark_source_covered(source, org)

    def mark_source_covered(self, source: str, org: str) -> None:
        """Record that a source was queried for an org, even when it returned zero records."""
        orgs = self.source_covered_orgs.setdefault(source, [])
        if org not in orgs:
            orgs.append(org)


def parse_ado_datetime(value: str | None) -> datetime | None:
    """Parse an Azure DevOps ISO datetime string into an aware UTC datetime."""
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def duration_minutes(start: str | datetime | None, finish: str | datetime | None) -> float | None:
    """Return positive duration in minutes, or None for missing/invalid timestamps."""
    start_dt = parse_ado_datetime(start) if isinstance(start, str) or start is None else start
    finish_dt = parse_ado_datetime(finish) if isinstance(finish, str) or finish is None else finish
    if not start_dt or not finish_dt or finish_dt < start_dt:
        return None
    return (finish_dt - start_dt).total_seconds() / 60.0


def month_key(value: str | datetime | date | None) -> str | None:
    """Return YYYY-MM for a date/datetime string or object."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return f"{value.year:04d}-{value.month:02d}"
    if isinstance(value, date):
        return f"{value.year:04d}-{value.month:02d}"
    parsed = parse_ado_datetime(str(value))
    if parsed:
        return f"{parsed.year:04d}-{parsed.month:02d}"
    text = str(value)
    return text[:7] if len(text) >= 7 else None
