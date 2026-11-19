"""Unit tests for Member C quantitative metrics, benchmarks, and scenario reporter."""

from __future__ import annotations

import pytest

from tools.metrics import (
    format_report_table,
    get_git_commit,
    measure_audit_throughput,
    measure_concurrency_budget_race,
    measure_decision_latency,
    measure_rules_evaluation,
    run_metrics,
    run_metrics_async,
)


def test_git_commit_detection() -> None:
    """Ensure git commit hash is safely resolved or defaults to unknown."""
    commit = get_git_commit()
    assert isinstance(commit, str)
    assert len(commit) >= 7 or commit == "unknown"


def test_rules_evaluation_denominators() -> None:
    """Validate rules-only evaluation against separate attack and normal datasets."""
    eval_res = measure_rules_evaluation()

    assert eval_res.attack_total == 20
    assert eval_res.attack_detected == 20
    assert eval_res.detection_rate == 1.0

    assert eval_res.normal_total == 25
    assert eval_res.normal_false_alarms == 0
    assert eval_res.false_alarm_rate == 0.0

    assert eval_res.precision == 1.0


def test_measured_decision_latency() -> None:
    """Verify decision pipeline latency benchmark satisfies the p95 < 100ms target."""
    latency = measure_decision_latency(trials=100)

    assert latency.sample_count == 100
    assert latency.mean_ms > 0
    assert latency.p50_ms <= latency.p95_ms <= latency.p99_ms <= latency.max_ms
    # Must meet the target: p95 under 100 ms locally
    assert latency.p95_ms < 100.0
    assert latency.target_achieved is True


@pytest.mark.asyncio
async def test_concurrency_budget_invariance_target() -> None:
    """Verify zero overspends across parallel race iterations."""
    concurrency = await measure_concurrency_budget_race(iterations=10)

    assert concurrency.total_runs == 10
    assert concurrency.overspend_runs == 0
    assert concurrency.target_achieved is True


def test_audit_verification_throughput_target() -> None:
    """Verify cryptographic audit verification throughput satisfies the < 1.0s target."""
    audit = measure_audit_throughput(block_count=1000)

    assert audit.block_count == 1000
    # Must verify 1,000 audit entries in under one second (1,000 ms)
    assert audit.elapsed_ms < 1000.0
    assert audit.target_achieved is True


@pytest.mark.asyncio
async def test_full_metrics_async_run() -> None:
    """Verify full end-to-end report generation across all 18 scenarios."""
    report = await run_metrics_async(trials=20, race_iterations=5, audit_blocks=100)

    assert report.model_version == "1.0.0-noisy_or"
    assert report.git_commit is not None
    assert len(report.scenario_outcomes) == 18

    # All 18 scenarios must be verified pass
    for sc in report.scenario_outcomes:
        assert sc.status == "PASS"
        assert sc.http_status == "PASS"
        assert sc.bank_ledger_status == "PASS"
        assert sc.mandate_usage_status == "PASS"
        assert sc.audit_trail_status == "PASS"

    formatted = format_report_table(report)
    assert "AGENTGATE QUANTITATIVE METRICS REPORT" in formatted
    assert "[SECTION 1] SCENARIO OUTCOME MATRIX" in formatted
    assert "[SECTION 2] RULES-ONLY DETECTION" in formatted
    assert "[SECTION 3] MEASURED DECISION PIPELINE LATENCY" in formatted
    assert "[SECTION 4] CONCURRENCY BUDGET INVARIANCE" in formatted
    assert "[SECTION 5] CRYPTOGRAPHIC AUDIT VERIFICATION THROUGHPUT" in formatted


def test_sync_runner_wrapper() -> None:
    """Ensure synchronous run_metrics wrapper executes properly."""
    report = run_metrics(trials=10, race_iterations=2, audit_blocks=50)
    assert len(report.scenario_outcomes) == 18
    assert report.evaluation_metrics.attack_detected == 20
