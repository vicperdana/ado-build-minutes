import asyncio
from datetime import datetime, timezone

from ado_build_minutes.analytics import (
    PARALLEL_CAPACITY_SOURCE,
    _collect_parallel_capacity,
    _collect_task_agent_snapshots,
    date_to_sk,
    parallel_capacity_apply,
    task_agent_apply,
    task_agent_slot_minutes,
)
from ado_build_minutes.classify import CAPACITY_NOT_MINUTES, VMSS_ELASTIC_POOL
from ado_build_minutes.models import PoolInfo


class FakeODataClient:
    async def get_json(self, url, params=None):
        return {
            "value": [{"SamplingTime": "2026-08-01T00:00:00Z", "IsHosted": False, "PoolId": 99, "MaxCount": 2}],
        }, {}


class FakeParallelCapacityClient:
    """Returns the entitlement rows ParallelPipelineJobsSnapshot actually produces."""

    async def get_json(self, url, params=None):
        return {
            "value": [
                {"IsHosted": True, "ParallelismTag": "Private", "MaxParallelJobs": 1, "MaxFreeMinutesGrant": 1800},
                {"IsHosted": False, "ParallelismTag": "Public", "MaxParallelJobs": 100000, "MaxFreeMinutesGrant": None},
            ],
        }, {}


def test_task_agent_slot_minutes_uses_max_count_times_ten_not_duration_sum():
    rows = [
        {"SamplingTime": "2026-08-01T00:00:00Z", "IsHosted": True, "PoolId": 1, "MaxCount": 3},
        {"SamplingTime": "2026-08-01T00:10:00Z", "IsHosted": True, "PoolId": 1, "MaxCount": 2},
        {"SamplingTime": "2026-08-01T00:20:00Z", "IsHosted": False, "PoolId": 2, "MaxCount": 1},
    ]
    assert task_agent_slot_minutes(rows) == 60.0


def test_date_to_sk():
    assert date_to_sk(datetime(2026, 8, 3, tzinfo=timezone.utc)) == 20260803


def test_task_agent_odata_query_preserves_safe_aggregation():
    query = task_agent_apply(datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 8, 31, tzinfo=timezone.utc))

    assert "IsRunning eq true" in query
    assert "aggregate($count as Count)" in query
    assert "aggregate(Count with max as MaxCount)" in query


def test_parallel_capacity_query_aggregates_entitlement_with_max_not_sum():
    query = parallel_capacity_apply(datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 8, 31, tzinfo=timezone.utc))

    assert "SamplingDate ge 2026-08-01Z" in query
    assert "SamplingDate le 2026-08-31Z" in query
    # Summing a re-sampled capacity constant multiplies it by the snapshot count.
    assert "with sum as" not in query
    assert "TotalCount with max as MaxParallelJobs" in query
    assert "TotalMinutes with max as MaxFreeMinutesGrant" in query
    # SamplingDate must not be a groupby key, or each snapshot becomes its own row.
    assert "groupby((IsHosted,ParallelismTag)" in query


def test_parallel_snapshot_entitlement_never_contributes_minutes_or_jobs():
    records = asyncio.run(
        _collect_parallel_capacity(
            FakeParallelCapacityClient(),
            "org",
            "project",
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 31, tzinfo=timezone.utc),
            [],
        )
    )

    assert len(records) == 2
    # The 1800-minute hosted grant and the 100000 public slot count are capacity, not usage.
    assert all(record.minutes == 0.0 for record in records)
    assert all(record.jobs == 0.0 for record in records)
    assert all(record.runner_type == CAPACITY_NOT_MINUTES for record in records)
    assert all(record.source == PARALLEL_CAPACITY_SOURCE for record in records)
    assert records[0].extra["hosted_free_minutes_grant"] == 1800
    assert records[1].extra["parallel_jobs_granted"] == 100000


def test_task_agent_rows_are_classified_with_pool_metadata_and_slots_not_jobs():
    pools = {99: PoolInfo(org="org", id=99, name="VMSS", runner_type=VMSS_ELASTIC_POOL)}
    records = asyncio.run(
        _collect_task_agent_snapshots(
            FakeODataClient(),
            "org",
            "project",
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            datetime(2026, 8, 31, tzinfo=timezone.utc),
            [],
            pools,
        )
    )

    assert records[0].runner_type == VMSS_ELASTIC_POOL
    assert records[0].minutes == 20
    assert records[0].jobs == 0
    assert records[0].extra["max_concurrent_slots"] == 2
