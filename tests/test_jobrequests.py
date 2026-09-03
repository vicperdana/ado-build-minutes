from ado_build_minutes.classify import MICROSOFT_HOSTED
from ado_build_minutes.jobrequests import jobrequest_to_record, parse_jobrequests_payload
from ado_build_minutes.models import PoolInfo


def _completed_request(**overrides):
    request = {
        "requestId": 42,
        "queueTime": "2026-08-01T00:00:00Z",
        "assignTime": "2026-08-01T00:02:00Z",
        "receiveTime": "2026-08-01T00:03:00Z",
        "finishTime": "2026-08-01T00:33:00Z",
        "agentSpecification": {"identifier": "ubuntu-latest"},
        "reservedAgent": {"name": "Azure Pipelines 12", "osDescription": "Ubuntu 22.04"},
        "data": {"ParallelismTag": "Private"},
        "definition": {"name": "CI"},
        "owner": {"name": "Build Service"},
        "result": "succeeded",
        "planType": "Build",
    }
    request.update(overrides)
    return request


def test_parse_jobrequests_handles_standard_value_shape():
    payload = {"count": 1, "value": [{"requestId": 1}]}
    assert parse_jobrequests_payload(payload) == [{"requestId": 1}]


def test_parse_jobrequests_handles_bare_array_top_shape():
    payload = [{"requestId": 1}, {"requestId": 2}]
    assert parse_jobrequests_payload(payload) == payload


def test_jobrequest_to_record_calculates_execution_and_queue_minutes_and_image():
    pool = PoolInfo(org="org", id=7, name="Azure Pipelines", runner_type=MICROSOFT_HOSTED)
    record = jobrequest_to_record("org", pool, _completed_request())
    assert record is not None
    assert record.minutes == 30
    assert record.queue_minutes == 2
    assert record.image == "ubuntu-latest"
    assert record.pipeline == "CI"


def test_jobrequest_to_record_excludes_incomplete_requests():
    pool = PoolInfo(org="org", id=7, name="Azure Pipelines", runner_type=MICROSOFT_HOSTED)

    assert jobrequest_to_record("org", pool, _completed_request(result=None)) is None
    assert jobrequest_to_record("org", pool, _completed_request(finishTime=None)) is None
    assert jobrequest_to_record("org", pool, _completed_request(receiveTime=None)) is None
    assert jobrequest_to_record("org", pool, _completed_request(assignTime=None)) is None
