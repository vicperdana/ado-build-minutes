import asyncio
from datetime import datetime, timezone
import shutil
from pathlib import Path

from ado_build_minutes.classify import MICROSOFT_HOSTED, UNKNOWN
from ado_build_minutes.models import PoolInfo, duration_minutes
from ado_build_minutes.timeline import _record_from_timeline_job, collect_timeline, dedupe_timeline_jobs

ARTIFACT_ROOT = Path(".test-output")


class FakeTimelineClient:
    def __init__(self):
        self.timeline_calls = 0

    async def get_continuation_pages(self, url, params=None, continuation=None, max_pages=10000):
        yield {"value": [{"name": "Project One"}]}, None

    async def get_json(self, url, params=None):
        if url.endswith("/_apis/distributedtask/pools"):
            return {"value": [{"id": 1, "name": "Azure Pipelines", "isHosted": True, "poolType": "automation"}]}, {}
        if url.endswith("/agents"):
            return {"value": []}, {}
        if url.endswith("/_apis/distributedtask/queues"):
            return {"value": [{"id": 10, "pool": {"id": 1, "name": "Azure Pipelines"}}]}, {}
        if url.endswith("/_apis/build/builds"):
            return {"value": [{"id": 123, "finishTime": "2026-08-01T00:30:00Z", "definition": {"name": "CI"}}]}, {}
        if url.endswith("/timeline"):
            self.timeline_calls += 1
            return {
                "records": [
                    {
                        "type": "Job",
                        "identifier": "job-a",
                        "attempt": 1,
                        "queueId": 10,
                        "startTime": "2026-08-01T00:00:00Z",
                        "finishTime": "2026-08-01T00:30:00Z",
                        "result": "succeeded",
                    }
                ]
            }, {}
        raise AssertionError(f"unexpected URL: {url}")


def test_duration_minutes_rejects_negative_or_missing_values():
    assert duration_minutes("2026-08-01T00:00:00Z", "2026-08-01T00:30:00Z") == 30
    assert duration_minutes("2026-08-01T00:30:00Z", "2026-08-01T00:00:00Z") is None
    assert duration_minutes(None, "2026-08-01T00:00:00Z") is None


def test_dedupe_timeline_jobs_by_identifier_and_attempt():
    records = [
        {"type": "Job", "identifier": "job-a", "attempt": 1},
        {"type": "Job", "identifier": "job-a", "attempt": 1},
        {"type": "Job", "identifier": "job-a", "attempt": 2},
        {"type": "Task", "identifier": "task-a", "attempt": 1},
    ]
    deduped = dedupe_timeline_jobs(records)
    assert len(deduped) == 2
    assert [item["attempt"] for item in deduped] == [1, 2]


def test_missing_timeline_queue_is_unknown_not_self_hosted():
    warnings = []
    record = _record_from_timeline_job(
        "org",
        "project",
        {"id": 5, "finishTime": "2026-08-01T00:30:00Z", "definition": {"name": "CI"}},
        {"type": "Job", "id": "job", "queueId": 999, "startTime": "2026-08-01T00:00:00Z", "finishTime": "2026-08-01T00:30:00Z"},
        {},
        {},
        warnings,
    )

    assert record is not None
    assert record.runner_type == UNKNOWN
    assert any("queueId=999" in warning for warning in warnings)


def test_timeline_checkpoint_resume_and_rerun_returns_persisted_records():
    shutil.rmtree(ARTIFACT_ROOT, ignore_errors=True)
    ARTIFACT_ROOT.mkdir(exist_ok=True)
    state_file = str(ARTIFACT_ROOT / "timeline-state.json")
    client = FakeTimelineClient()

    first = asyncio.run(collect_timeline(client, ["org"], datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 8, 31, tzinfo=timezone.utc), state_file))
    second = asyncio.run(collect_timeline(client, ["org"], datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 8, 31, tzinfo=timezone.utc), state_file))

    assert len(first.records) == 1
    assert first.records[0].runner_type == MICROSOFT_HOSTED
    assert len(second.records) == 1
    assert second.records[0].build_id == 123
    assert client.timeline_calls == 1
