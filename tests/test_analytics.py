import asyncio
from datetime import datetime, timezone

from ado_build_minutes.analytics import (
    _collect_task_agent_snapshots,
    date_to_sk,
    parallel_snapshot_apply,
    task_agent_apply,
    task_agent_slot_minutes,
)
from ado_build_minutes.classify import VMSS_ELASTIC_POOL
from ado_build_minutes.models import PoolInfo


class FakeODataClient:
    async def get_json(self, url, params=None):
        return {
            "value": [{"SamplingTime": "2026-08-01T00:00:00Z", "IsHosted": False, "PoolId": 99, "MaxCount": 2}],
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


def test_parallel_odata_query_uses_inclusive_daily_bounds():
    query = parallel_snapshot_apply(datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 8, 31, tzinfo=timezone.utc))

    assert "SamplingDate ge 2026-08-01Z" in query
    assert "SamplingDate le 2026-08-31Z" in query


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
