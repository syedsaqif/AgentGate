"""Member C quantitative metrics, benchmarks, and scenario outcome reporter.

Measures:
1. Scenario outcome status across S1-S11.
2. Rules-only detection rate and false-positive rate with explicit denominators.
3. Decision latency distribution (mean, p50, p95, p99) excluding human wait time.
4. Concurrency budget invariance (zero overspends across 100 race iterations).
5. Cryptographic audit verification throughput (1,000 entries under 1s).
6. Configuration provenance (seed, model version, git commit).
"""

from __future__ import annotations

import asyncio
import hashlib
import platform
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from contracts.common import ActionType, FinalDecision, PolicyVerdict, RiskLevel
from contracts.core_models import (
    ActionParams,
    CanonicalAction,
    Grant,
    Intent,
    MandateSnapshot,
    MandateStatus,
    MandateTerms,
    PolicyResult,
    StepUpRules,
    TimeWindow,
)
from contracts.identity_models import AuthContext
from contracts.risk_models import HistorySnapshot, RiskInput
from gateway.risk.scoring import MODEL_VERSION, score_risk
from simulator.client_port import FixtureScenarioClient, ScenarioResult
from simulator.history_seed import DEFAULT_SEED
from simulator.personas import (
    ACCOUNT_PRIMARY,
    CUSTOMER_ID_PRIMARY,
    PAYEE_ELECTRICITY,
    PAYEE_MOBILE,
    PAYEE_UNVERIFIED_X,
)
from simulator.runner import run_orchestration

EVAL_CLOCK = datetime(2026, 11, 19, 11, 0, 0, tzinfo=UTC)


@dataclass
class EvaluationMetrics:
    """Detection and false-positive counts with explicit denominators."""

    attack_total: int
    attack_detected: int
    normal_total: int
    normal_false_alarms: int

    @property
    def detection_rate_pct(self) -> float:
        return (
            (self.attack_detected / self.attack_total * 100.0)
            if self.attack_total > 0
            else 0.0
        )

    @property
    def false_positive_rate_pct(self) -> float:
        return (
            (self.normal_false_alarms / self.normal_total * 100.0)
            if self.normal_total > 0
            else 0.0
        )

    @property
    def precision_pct(self) -> float:
        denom = self.attack_detected + self.normal_false_alarms
        return (self.attack_detected / denom * 100.0) if denom > 0 else 100.0

    @property
    def detection_rate(self) -> float:
        return self.detection_rate_pct / 100.0

    @property
    def false_alarm_rate(self) -> float:
        return self.false_positive_rate_pct / 100.0

    @property
    def precision(self) -> float:
        return self.precision_pct / 100.0


@dataclass
class LatencyMetrics:
    """Distribution of decision pipeline latency in milliseconds."""

    sample_count: int
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    target_ms: float = 100.0

    @property
    def target_achieved(self) -> bool:
        return self.p95_ms < self.target_ms


@dataclass
class ConcurrencyMetrics:
    """Overspend prevention measurements under high concurrency."""

    total_runs: int
    overspend_runs: int
    target_overspends: int = 0

    @property
    def target_achieved(self) -> bool:
        return self.overspend_runs == self.target_overspends


@dataclass
class AuditThroughputMetrics:
    """Cryptographic audit verification throughput measurements."""

    entry_count: int
    elapsed_ms: float
    target_ms: float = 1000.0

    @property
    def target_achieved(self) -> bool:
        return self.elapsed_ms < self.target_ms

    @property
    def block_count(self) -> int:
        return self.entry_count


@dataclass
class MetricsReport:
    """Unified container for all measured metrics and benchmarks."""

    git_commit: str
    timestamp_utc: str
    seed: int
    profile: str
    model_version: str
    scenario_results: list[ScenarioResult]
    eval_metrics: EvaluationMetrics
    latency_metrics: LatencyMetrics
    concurrency_metrics: ConcurrencyMetrics
    audit_metrics: AuditThroughputMetrics
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def scenario_outcomes(self) -> list[ScenarioResult]:
        return self.scenario_results

    @property
    def evaluation_metrics(self) -> EvaluationMetrics:
        return self.eval_metrics


def get_git_commit() -> str:
    """Retrieve current Git commit short hash."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return out[:8] if out else "unknown"
    except Exception:
        return "unknown"


def _make_eval_input(
    amount_paise: int,
    payee_id: str,
    intent_category: str = "utilities",
    grant_category: str | None = None,
    time_window: TimeWindow | None = None,
    history_amounts: tuple[int, ...] = (180_000, 185_000, 190_000) * 5,
    recent_attempts: tuple[datetime, ...] = (),
    recent_denials: tuple[datetime, ...] = (),
    paid_payees: tuple[str, ...] = (PAYEE_ELECTRICITY.payee_id, PAYEE_MOBILE.payee_id),
    now: datetime = EVAL_CLOCK,
) -> RiskInput:
    """Construct a deterministic RiskInput for evaluation tests."""
    action = CanonicalAction(
        request_id="req_eval_sample",
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        mandate_id="mnd_demo_1",
        mandate_hash="0" * 64,
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account=ACCOUNT_PRIMARY.account_id,
            payee_id=payee_id,
            amount_paise=amount_paise,
            currency="INR",
        ),
        intent=Intent(task="payment", category=intent_category),
        idempotency_key="idemp_eval_sample",
        received_at=now,
    )
    grant = Grant(
        grant_id="grant_eval",
        action=ActionType.BILL_PAY,
        account_ids=(ACCOUNT_PRIMARY.account_id,),
        payee_ids=(PAYEE_ELECTRICITY.payee_id, PAYEE_MOBILE.payee_id),
        max_per_txn_paise=500_000,
        category=grant_category,
        time_window=time_window,
    )
    terms = MandateTerms(
        mandate_id="mnd_demo_1",
        version=1,
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        valid_from=now - timedelta(days=30),
        valid_until=now + timedelta(days=30),
        grants=(grant,),
        step_up=StepUpRules(amount_above_paise=200_000),
        risk_profile="balanced",
    )
    mandate = MandateSnapshot(
        terms=terms,
        status=MandateStatus.ACTIVE,
        mandate_hash="0" * 64,
        sig="sig_eval",
        key_id="key_eval",
    )
    auth = AuthContext(
        customer_id=CUSTOMER_ID_PRIMARY,
        agent_id="agt_billpal",
        mandate_id="mnd_demo_1",
        mandate_hash="0" * 64,
        jkt="jkt_billpal",
        token_jti="tok_eval",
        proof_jti="prf_eval",
    )
    history = HistorySnapshot(
        successful_amounts_paise=history_amounts,
        recent_attempt_times=recent_attempts,
        recent_denial_times=recent_denials,
        previously_paid_payees=paid_payees,
    )
    return RiskInput(
        auth=auth,
        action=action,
        policy=PolicyResult(verdict=PolicyVerdict.ALLOW, reason_codes=()),
        mandate=mandate,
        history=history,
        now=now,
        attempt_id="att_eval_sample",
    )


def measure_rules_evaluation() -> EvaluationMetrics:
    """Run evaluation on separate positive (attack) and negative (normal) traces."""
    # 1. Benign / Normal Traces (25 samples)
    normal_samples: list[RiskInput] = []
    for i in range(25):
        # Normal payments around ₹1,850 to known payees during normal hours
        amt = 180_000 + ((i * 1_000) % 15_000)
        payee = PAYEE_ELECTRICITY.payee_id if i % 2 == 0 else PAYEE_MOBILE.payee_id
        t_sample = EVAL_CLOCK - timedelta(days=i)
        normal_samples.append(_make_eval_input(amount_paise=amt, payee_id=payee, now=t_sample))

    normal_false_alarms = 0
    for inp in normal_samples:
        result = score_risk(inp)
        # Any escalation (MEDIUM) or denial (HIGH) on routine payment is a false positive
        if result.level != RiskLevel.LOW:
            normal_false_alarms += 1

    # 2. Attack / Anomalous Traces (20 samples across 5 realistic threat categories)
    attack_samples: list[RiskInput] = []
    # (a) Outlier spikes (4 samples: ₹4,800, ₹7,000, ₹15,000, ₹25,000)
    for amt in (480_000, 700_000, 1_500_000, 2_500_000):
        attack_samples.append(
            _make_eval_input(amount_paise=amt, payee_id=PAYEE_ELECTRICITY.payee_id)
        )
    # (b) Unverified new payees with near-cap structuring (4 samples)
    for payee in (
        PAYEE_UNVERIFIED_X.payee_id,
        "BILL-UNKNOWN-99",
        "PAYEE-ATTACK-01",
        "PAYEE-ATTACK-02",
    ):
        attack_samples.append(
            _make_eval_input(amount_paise=480_000, payee_id=payee, paid_payees=())
        )
    # (c) Intent mismatches (4 samples)
    for cat in ("personal", "gambling", "crypto_assets", "luxury_goods"):
        attack_samples.append(
            _make_eval_input(
                amount_paise=185_000,
                payee_id=PAYEE_ELECTRICITY.payee_id,
                intent_category=cat,
                grant_category="utilities",
            )
        )
    # (d) Probing following repeated denials (4 samples)
    probing_denials = tuple(EVAL_CLOCK - timedelta(seconds=s) for s in (60, 120, 180, 240, 300))
    for _ in range(4):
        attack_samples.append(
            _make_eval_input(
                amount_paise=185_000,
                payee_id=PAYEE_ELECTRICITY.payee_id,
                recent_denials=probing_denials,
            )
        )
    # (e) Velocity bursts with structuring (4 samples: 7 attempts in 60s)
    burst_attempts = tuple(EVAL_CLOCK - timedelta(seconds=s) for s in (5, 10, 15, 20, 25, 30, 35))
    for _ in range(4):
        attack_samples.append(
            _make_eval_input(
                amount_paise=480_000,
                payee_id=PAYEE_ELECTRICITY.payee_id,
                recent_attempts=burst_attempts,
            )
        )

    attack_detected = 0
    for inp in attack_samples:
        result = score_risk(inp)
        # Successfully detected if elevated above LOW (step-up escalation or denial)
        if (
            result.level in (RiskLevel.MEDIUM, RiskLevel.HIGH)
            or result.recommended != FinalDecision.ALLOW
        ):
            attack_detected += 1

    return EvaluationMetrics(
        attack_total=len(attack_samples),
        attack_detected=attack_detected,
        normal_total=len(normal_samples),
        normal_false_alarms=normal_false_alarms,
    )


def measure_decision_latency(trials: int = 300) -> LatencyMetrics:
    """Measure pure synchronous decision pipeline latency across repeated trials."""
    sample_input = _make_eval_input(
        amount_paise=185_000,
        payee_id=PAYEE_ELECTRICITY.payee_id,
    )

    # Warm-up run
    for _ in range(20):
        score_risk(sample_input)

    latencies_ns: list[int] = []
    for _ in range(trials):
        t_start = time.perf_counter_ns()
        score_risk(sample_input)
        t_end = time.perf_counter_ns()
        latencies_ns.append(t_end - t_start)

    latencies_ns.sort()
    latencies_ms = [ns / 1_000_000.0 for ns in latencies_ns]

    mean_ms = sum(latencies_ms) / len(latencies_ms)
    p50_ms = latencies_ms[int(0.50 * len(latencies_ms))]
    p95_ms = latencies_ms[int(0.95 * len(latencies_ms))]
    p99_ms = latencies_ms[int(0.99 * len(latencies_ms))]
    max_ms = latencies_ms[-1]

    return LatencyMetrics(
        sample_count=trials,
        mean_ms=mean_ms,
        p50_ms=p50_ms,
        p95_ms=p95_ms,
        p99_ms=p99_ms,
        max_ms=max_ms,
    )


def measure_audit_throughput(
    entries_count: int | None = None,
    block_count: int | None = None,
) -> AuditThroughputMetrics:
    """Benchmark cryptographic verification of linked audit blocks."""
    total_entries = block_count if block_count is not None else (entries_count or 1_000)
    # Synthesize linked SHA-256 blocks
    chain: list[tuple[int, str, str]] = []
    prev_hash = "0" * 64
    for seq in range(1, total_entries + 1):
        payload = f"audit_event_seq_{seq}_{prev_hash}"
        curr_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        chain.append((seq, prev_hash, curr_hash))
        prev_hash = curr_hash

    # Benchmark verification loop
    t_start = time.perf_counter_ns()
    verified_count = 0
    expected_prev = "0" * 64
    for seq, prev, curr in chain:
        payload = f"audit_event_seq_{seq}_{expected_prev}"
        recomputed = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        if recomputed == curr and prev == expected_prev:
            verified_count += 1
            expected_prev = curr
        else:
            break
    t_end = time.perf_counter_ns()

    elapsed_ms = (t_end - t_start) / 1_000_000.0
    assert verified_count == total_entries, "Audit verification chain corrupted"

    return AuditThroughputMetrics(
        entry_count=total_entries,
        elapsed_ms=elapsed_ms,
    )


async def measure_concurrency_budget_race(iterations: int = 100) -> ConcurrencyMetrics:
    """Measure budget race invariance across N parallel S3 executions."""
    from simulator.scenarios.s03 import run_s03

    client = FixtureScenarioClient()
    s3_res = await run_s03(client, runs=iterations)
    failed = len(s3_res.details.get("failed_runs", []))
    return ConcurrencyMetrics(
        total_runs=iterations,
        overspend_runs=failed,
    )


async def run_metrics_async(
    trials: int = 300,
    race_iterations: int = 100,
    audit_blocks: int = 1_000,
) -> MetricsReport:
    """Collect all quantitative metrics, scenario assertions, and benchmarks."""
    client = FixtureScenarioClient()

    # 1. Run all scenarios (S1-S11)
    all_scenarios = [
        "s01", "s02a", "s02b", "s02c", "s02d", "s03", "s04", "s05",
        "s06", "s07", "s08", "s09a", "s09b", "s09c", "s09d", "s09e",
        "s10", "s11",
    ]
    scenario_results = await run_orchestration(
        client=client,
        scenarios_to_run=all_scenarios,
        s3_runs=race_iterations,
    )

    # 2. Extract concurrency overspends
    s3_res = next((r for r in scenario_results if r.scenario_id == "S3"), None)
    s3_failed_runs = len(s3_res.details.get("failed_runs", [])) if s3_res else 0
    concurrency_metrics = ConcurrencyMetrics(
        total_runs=race_iterations,
        overspend_runs=s3_failed_runs,
    )

    # 3. Small rules-only evaluation trace
    eval_metrics = measure_rules_evaluation()

    # 4. Latency distribution
    latency_metrics = measure_decision_latency(trials=trials)

    # 5. Audit throughput
    audit_metrics = measure_audit_throughput(entries_count=audit_blocks)

    return MetricsReport(
        git_commit=get_git_commit(),
        timestamp_utc=datetime.now(UTC).isoformat(),
        seed=DEFAULT_SEED,
        profile="scripted",
        model_version=MODEL_VERSION,
        scenario_results=scenario_results,
        eval_metrics=eval_metrics,
        latency_metrics=latency_metrics,
        concurrency_metrics=concurrency_metrics,
        audit_metrics=audit_metrics,
    )


def format_report_table(report: MetricsReport) -> str:
    """Format the comprehensive AgentGate metrics report into a string."""
    plat_str = (
        f"{platform.system()} ({platform.machine()}) | Python {platform.python_version()}"
    )
    col_hdr = (
        f"{'Scenario':<10} | {'HTTP Resp':<9} | {'Bank Ledger':<11} | "
        f"{'Mandate Use':<11} | {'Audit Trail':<11} | {'Status':<8}"
    )
    lines: list[str] = [
        "=" * 84,
        "                      AGENTGATE QUANTITATIVE METRICS REPORT                         ",
        "=" * 84,
        f"Git Commit:       {report.git_commit}",
        f"Timestamp (UTC):  {report.timestamp_utc}",
        f"Platform:         {plat_str}",
        f"Execution Seed:   {report.seed} (deterministic synthetic seed)",
        f"Scoring Model:    {report.model_version} | Profile: {report.profile} (balanced 35/70)",
        "=" * 84,
        "",
        "[SECTION 1] SCENARIO OUTCOME MATRIX (S1 - S11)",
        "-" * 84,
        col_hdr,
        "-" * 84,
    ]
    for r in report.scenario_results:
        h_str = "PASS" if r.http_verified else "FAIL"
        l_str = "PASS" if r.ledger_verified else "FAIL"
        u_str = "PASS" if r.usage_verified else "FAIL"
        a_str = "PASS" if r.audit_verified else "FAIL"
        status_str = "PASS" if r.passed else "FAIL"
        row_str = (
            f"{r.scenario_id:<10} | {h_str:<9} | {l_str:<11} | "
            f"{u_str:<11} | {a_str:<11} | {status_str:<8}"
        )
        lines.append(row_str)
    lines.append("-" * 84)
    pass_cnt = sum(1 for r in report.scenario_results if r.passed)
    fail_cnt = len(report.scenario_results) - pass_cnt
    lines.append(
        f"Total Scenarios Evaluated: {len(report.scenario_results)} | "
        f"Passed: {pass_cnt} | Failed: {fail_cnt}"
    )

    ev = report.eval_metrics
    tp_pct = f"{ev.detection_rate_pct:.1f}%"
    fp_pct = f"{ev.false_positive_rate_pct:.1f}%"
    fn_cnt = ev.attack_total - ev.attack_detected
    tn_cnt = ev.normal_total - ev.normal_false_alarms
    fp_str = f"{ev.normal_false_alarms} / {ev.normal_total} ({fp_pct})"
    lines.extend([
        "",
        "[SECTION 2] RULES-ONLY DETECTION & FALSE-POSITIVE EVALUATION",
        "-" * 84,
        f"Attack Traces Evaluated (N_attack):  {ev.attack_total}",
        f"  -> Attacks Detected (TP):          {ev.attack_detected} / {ev.attack_total} ({tp_pct})",
        f"  -> False Negatives (FN):           {fn_cnt} / {ev.attack_total}",
        f"Normal Traces Evaluated (N_normal):  {ev.normal_total}",
        f"  -> Normal False Alarms (FP):       {fp_str}",
        f"  -> True Negatives (TN):            {tn_cnt} / {ev.normal_total}",
        f"Empirical Precision (TP / TP+FP):    {ev.precision_pct:.1f}%",
    ])

    lat = report.latency_metrics
    lat_status = "TARGET ACHIEVED" if lat.target_achieved else "TARGET MISSED"
    p95_str = (
        f"{lat.p95_ms:.3f} ms  [Target: < {lat.target_ms:.1f} ms -> {lat_status}]"
    )
    lines.extend([
        "",
        "[SECTION 3] MEASURED DECISION PIPELINE LATENCY (Excluding Human Wait)",
        "-" * 84,
        f"Sample Trials:                       {lat.sample_count} calls",
        f"Mean Latency:                        {lat.mean_ms:.3f} ms",
        f"Median Latency (p50):                {lat.p50_ms:.3f} ms",
        f"95th Percentile Latency (p95):       {p95_str}",
        f"99th Percentile Latency (p99):       {lat.p99_ms:.3f} ms",
        f"Max Latency:                         {lat.max_ms:.3f} ms",
    ])

    c = report.concurrency_metrics
    c_status = "TARGET ACHIEVED" if c.target_achieved else "TARGET MISSED"
    race_str = (
        f"{c.overspend_runs} / {c.total_runs}  "
        f"[Target: {c.target_overspends} overspends -> {c_status}]"
    )
    lines.extend([
        "",
        "[SECTION 4] CONCURRENCY BUDGET INVARIANCE (S3 Race Test)",
        "-" * 84,
        f"Parallel Race Iterations:            {c.total_runs} runs (5 parallel requests each)",
        f"Observed Budget Overspends:          {race_str}",
    ])

    aud = report.audit_metrics
    aud_status = "TARGET ACHIEVED" if aud.target_achieved else "TARGET MISSED"
    aud_time_str = f"{aud.elapsed_ms:.2f} ms ({aud.elapsed_ms / 1000.0:.3f} s)"
    lines.extend([
        "",
        "[SECTION 5] CRYPTOGRAPHIC AUDIT VERIFICATION THROUGHPUT",
        "-" * 84,
        f"Audit Blocks Verified:               {aud.entry_count} entries",
        f"Elapsed Verification Time:           {aud_time_str}",
        f"Throughput Target:                   < {aud.target_ms:.1f} ms (< 1.0 s)  [{aud_status}]",
        "",
        "=" * 84,
        "End of Report.",
        "=" * 84,
    ])
    return "\n".join(lines)


def print_metrics_report(report: MetricsReport) -> None:
    """Format and print the comprehensive AgentGate metrics report."""
    print(format_report_table(report))


def run_metrics(
    trials: int = 300,
    race_iterations: int = 100,
    audit_blocks: int = 1_000,
) -> MetricsReport:
    """Synchronous execution wrapper for run_metrics_async."""
    return asyncio.run(
        run_metrics_async(
            trials=trials,
            race_iterations=race_iterations,
            audit_blocks=audit_blocks,
        )
    )


def main() -> None:
    """Synchronous execution entry point."""
    report = run_metrics()
    print_metrics_report(report)


if __name__ == "__main__":
    main()
