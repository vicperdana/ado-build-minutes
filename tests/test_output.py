import shutil
from pathlib import Path

from ado_build_minutes.classify import CAPACITY_NOT_MINUTES, MICROSOFT_HOSTED, SELF_HOSTED, UNKNOWN
from ado_build_minutes.models import CollectionResult, Failure, UsageRecord
from ado_build_minutes.output import actions_cost_rows, choose_primary_source, write_outputs

ARTIFACT_ROOT = Path(".test-output")


def clean_artifacts():
    shutil.rmtree(ARTIFACT_ROOT, ignore_errors=True)
    ARTIFACT_ROOT.mkdir(exist_ok=True)


def test_source_selection_requires_complete_coverage():
    records = [
        UsageRecord(source="jobrequests", org="org-a", runner_type=MICROSOFT_HOSTED, minutes=10),
        UsageRecord(source="analytics_taskagent_slots", org="org-a", runner_type=MICROSOFT_HOSTED, minutes=20),
        UsageRecord(source="analytics_taskagent_slots", org="org-b", runner_type=SELF_HOSTED, minutes=30),
    ]
    assert choose_primary_source(records, ["org-a", "org-b"], []) == "analytics_taskagent_slots"
    assert choose_primary_source(records[:1], ["org-a", "org-b"], [], "jobrequests") is None


def test_partial_coverage_markdown_has_prominent_warning_and_no_clean_total():
    clean_artifacts()
    result = CollectionResult(
        records=[UsageRecord(source="analytics_taskagent_slots", org="org-a", runner_type=MICROSOFT_HOSTED, minutes=20)],
        failures=[Failure("analytics_taskagent_slots", "org-b", "proj", 403, "denied")],
        expected_orgs=["org-a", "org-b"],
    )
    written = write_outputs(result, str(ARTIFACT_ROOT / "partial"), "markdown", "2026-08-01", "2026-08-31")
    summary = Path(written["written"][0]).read_text()

    assert written["primary_source"] is None
    assert "MIXED PROVENANCE / INCOMPLETE COVERAGE" in summary
    assert "org-b" in summary
    assert "403" in summary
    assert "clean combined total is intentionally not shown" in summary
    assert "_No rows._" in summary


def test_billing_reconciliation_omits_percentage_for_non_comparable_periods():
    clean_artifacts()
    result = CollectionResult(
        records=[
            UsageRecord(source="analytics_taskagent_slots", org="org-a", runner_type=MICROSOFT_HOSTED, minutes=100),
            UsageRecord(source="billing_unsupported", org="org-a", runner_type=MICROSOFT_HOSTED, minutes=80),
        ],
        expected_orgs=["org-a"],
    )
    written = write_outputs(result, str(ARTIFACT_ROOT / "billing"), "markdown", "2026-08-01", "2026-08-31")
    summary = Path(written["written"][0]).read_text()

    assert "not directly comparable" in summary
    assert "Variance:" not in summary


def test_actions_cost_leaves_unknown_hosted_os_unpriced():
    rows, warnings = actions_cost_rows(
        [UsageRecord(source="analytics_taskagent_slots", org="org-a", runner_type=MICROSOFT_HOSTED, minutes=60)],
        {"linux_per_minute": 0.008},
        "analytics_taskagent_slots",
    )

    assert rows == [{"runner_type": MICROSOFT_HOSTED, "rate_key": "unknown_hosted_os", "minutes": 60.0, "rate": "unpriced", "estimated_cost": "unpriced"}]
    assert any("unknown_hosted_os" in warning for warning in warnings)


def test_unknown_runner_type_is_reported_in_summary():
    clean_artifacts()
    result = CollectionResult(
        records=[UsageRecord(source="timeline", org="org-a", runner_type=UNKNOWN, minutes=12)],
        expected_orgs=["org-a"],
    )
    written = write_outputs(result, str(ARTIFACT_ROOT / "unknown"), "markdown", "2026-08-01", "2026-08-31", explicit_headline_source="timeline")
    summary = Path(written["written"][0]).read_text()

    assert "unknown" in summary
    assert "Unclassified minutes: 12.0" in summary


def test_capacity_sources_are_never_selected_as_headline():
    """Entitlement/capacity records must never become the headline usage total."""
    records = [
        UsageRecord(source="analytics_parallel_capacity", org="org-a", runner_type=CAPACITY_NOT_MINUTES, minutes=0, jobs=0),
        UsageRecord(source="billing_resourceusage_capacity", org="org-a", runner_type=CAPACITY_NOT_MINUTES, minutes=0, jobs=0),
    ]

    assert choose_primary_source(records, ["org-a"], []) is None
    # Even an explicit request must not promote a capacity source.
    assert choose_primary_source(records, ["org-a"], [], "analytics_parallel_capacity") is None


def test_capacity_records_do_not_inflate_headline_totals():
    clean_artifacts()
    result = CollectionResult(
        records=[
            UsageRecord(source="analytics_taskagent_slots", org="org-a", runner_type=MICROSOFT_HOSTED, minutes=30, jobs=0),
            UsageRecord(
                source="analytics_parallel_capacity",
                org="org-a",
                runner_type=CAPACITY_NOT_MINUTES,
                minutes=0,
                jobs=0,
                extra={"parallel_jobs_granted": 100000, "hosted_free_minutes_grant": 1800},
            ),
        ],
        expected_orgs=["org-a"],
    )
    written = write_outputs(result, str(ARTIFACT_ROOT / "capacity"), "json", "2026-08-01", "2026-08-31")
    summary = written["summary"]

    assert written["primary_source"] == "analytics_taskagent_slots"
    org_totals = {row["org"]: row for row in summary["summaries"]["by_org"]}
    assert org_totals["org-a"]["minutes"] == 30.0
    assert org_totals["org-a"]["jobs"] == 0.0
