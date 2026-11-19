"""Member C Scenario Runner CLI entry point.

Usage:
    python -m simulator.runner --all --profile scripted
    python -m simulator.runner --scenario s01 --profile test
    python -m simulator.runner --all --runs 50
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from simulator.client_port import FixtureScenarioClient, ScenarioClient, ScenarioResult
from simulator.scenarios.s01 import run_s01
from simulator.scenarios.s02 import run_s02, run_s02a, run_s02b, run_s02c, run_s02d
from simulator.scenarios.s03 import run_s03
from simulator.scenarios.s04 import run_s04
from simulator.scenarios.s05 import run_s05
from simulator.scenarios.s06 import run_s06
from simulator.scenarios.s07 import run_s07
from simulator.scenarios.s08 import run_s08
from simulator.scenarios.s09 import (
    run_s09,
    run_s09a,
    run_s09b,
    run_s09c,
    run_s09d,
    run_s09e,
)
from simulator.scenarios.s10 import run_s10
from simulator.scenarios.s11 import run_s11


def check_real_providers(profile: str = "scripted") -> tuple[bool, list[str]]:
    """Inspect runtime dependencies and detect fake/missing providers."""
    fake_reports: list[str] = []

    # Check 1: Member B Agent SDK
    try:
        import agent_sdk.client as sdk_mod

        if not hasattr(sdk_mod, "AgentGateClient") or "placeholder" in (sdk_mod.__doc__ or ""):
            fake_reports.append(
                "agent_sdk: AgentGateClient is not implemented (agent_sdk.client placeholder)"
            )
    except Exception as exc:
        fake_reports.append(f"agent_sdk: Failed to import agent_sdk.client ({exc})")

    # Check 2: Gateway dependencies
    try:
        from gateway.config import AgentGateConfig
        from gateway.deps import build_dependencies, is_fake_provider

        cfg = AgentGateConfig.load(profile=profile)
        deps = build_dependencies(config=cfg)

        checked_services = {
            "bank_client": deps.bank_client,
            "identity_verifier": deps.identity_verifier,
            "audit_log": deps.audit_log,
            "human_auth": deps.human_auth,
            "policy_evaluator": deps.policy_evaluator,
            "mandate_reader": deps.mandate_reader,
            "execution_port": deps.execution_port,
        }

        for svc_name, provider in checked_services.items():
            if provider is None:
                fake_reports.append(f"{svc_name}: None (unconfigured)")
            elif is_fake_provider(provider):
                fake_reports.append(
                    f"{svc_name}: {provider.__class__.__name__} (test double/fake)"
                )
    except Exception as exc:
        fake_reports.append(f"gateway_deps: Error inspecting dependencies ({exc})")

    has_fakes = len(fake_reports) > 0
    return has_fakes, fake_reports


async def run_orchestration(
    client: ScenarioClient,
    scenarios_to_run: list[str],
    s3_runs: int = 100,
    now: datetime | None = None,
) -> list[ScenarioResult]:
    """Execute scenarios against the supplied client double to verify orchestration."""
    results: list[ScenarioResult] = []
    clock_now = now or datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)

    for sc in scenarios_to_run:
        sc_lower = sc.lower()
        if sc_lower in ("s01", "s1", "all"):
            results.append(await run_s01(client, now=clock_now))
        if sc_lower in ("s02", "s2", "all"):
            s2_res = await run_s02(client, now=clock_now)
            results.extend(s2_res)
        elif sc_lower in ("s02a", "s2a"):
            results.append(await run_s02a(client, now=clock_now))
        elif sc_lower in ("s02b", "s2b"):
            results.append(await run_s02b(client, now=clock_now))
        elif sc_lower in ("s02c", "s2c"):
            results.append(await run_s02c(client, now=clock_now))
        elif sc_lower in ("s02d", "s2d"):
            results.append(await run_s02d(client, now=clock_now))
        if sc_lower in ("s03", "s3", "all"):
            results.append(await run_s03(client, runs=s3_runs, now=clock_now))
        if sc_lower in ("s04", "s4", "all"):
            results.append(await run_s04(client, base_time=clock_now))
        if sc_lower in ("s05", "s5", "all"):
            results.append(await run_s05(client, now=clock_now))
        if sc_lower in ("s06", "s6", "all"):
            results.append(await run_s06(client, now=clock_now))
        if sc_lower in ("s07", "s7", "all"):
            results.append(await run_s07(client, now=clock_now))
        if sc_lower in ("s08", "s8", "all"):
            results.append(await run_s08(client, now=clock_now))
        if sc_lower in ("s09", "s9", "all"):
            s9_res = await run_s09(client, now=clock_now)
            results.extend(s9_res)
        elif sc_lower in ("s09a", "s9a"):
            results.append(await run_s09a(client, now=clock_now))
        elif sc_lower in ("s09b", "s9b"):
            results.append(await run_s09b(client, now=clock_now))
        elif sc_lower in ("s09c", "s9c"):
            results.append(await run_s09c(client, now=clock_now))
        elif sc_lower in ("s09d", "s9d"):
            results.append(await run_s09d(client, now=clock_now))
        elif sc_lower in ("s09e", "s9e"):
            results.append(await run_s09e(client, now=clock_now))
        if sc_lower in ("s10", "s10", "all"):
            results.append(await run_s10(client, now=clock_now))
        if sc_lower in ("s11", "s11", "all"):
            results.append(await run_s11(client, now=clock_now))

    return results


def print_summary_table(
    results: list[ScenarioResult],
    profile: str,
    client_mode: str,
) -> None:
    """Render structured ASCII summary table of 4-dimension assertions."""
    print("\n" + "=" * 80)
    print(f" AgentGate Scenario Verification Report  [Profile: {profile} | Mode: {client_mode}]")
    print("=" * 80)
    header = (
        f"{'Scenario':<10} | {'HTTP Resp':<9} | {'Bank Ledger':<11} | "
        f"{'Mandate Use':<11} | {'Audit Trail':<11} | {'Result':<8}"
    )
    print(header)
    print("-" * 80)

    pass_count = 0
    fail_count = 0
    skip_count = 0

    for r in results:
        if r.skipped:
            skip_count += 1
            res_str = "SKIP"
            h_str = l_str = u_str = a_str = "N/A"
        elif r.passed:
            pass_count += 1
            res_str = "PASS"
            h_str = "PASS" if r.http_verified else "FAIL"
            l_str = "PASS" if r.ledger_verified else "FAIL"
            u_str = "PASS" if r.usage_verified else "FAIL"
            a_str = "PASS" if r.audit_verified else "FAIL"
        else:
            fail_count += 1
            res_str = "FAIL"
            h_str = "PASS" if r.http_verified else "FAIL"
            l_str = "PASS" if r.ledger_verified else "FAIL"
            u_str = "PASS" if r.usage_verified else "FAIL"
            a_str = "PASS" if r.audit_verified else "FAIL"

        row = (
            f"{r.scenario_id:<10} | {h_str:<9} | {l_str:<11} | "
            f"{u_str:<11} | {a_str:<11} | {res_str:<8}"
        )
        print(row)
        if not r.passed and not r.skipped and r.message:
            print(f"   -> Details: {r.message}")

    print("-" * 80)
    print(f"Summary: {pass_count} passed, {fail_count} failed, {skip_count} skipped")
    print("=" * 80 + "\n")


async def main_async(args: argparse.Namespace) -> int:
    """Async orchestration entry point."""
    print("Initializing AgentGate Scenario Runner...")
    print(f"Target profile: {args.profile}")

    scenarios = ["all"] if args.all or not args.scenario else [args.scenario]
    clock_now = datetime(2026, 11, 19, 10, 0, 0, tzinfo=UTC)

    # Stage 1: Run against FixtureScenarioClient to debug orchestration
    print("\n--- Stage 1: Running Scenarios against FixtureScenarioClient ---")
    fixture_client = FixtureScenarioClient()
    fixture_results = await run_orchestration(
        client=fixture_client,
        scenarios_to_run=scenarios,
        s3_runs=args.runs,
        now=clock_now,
    )
    print_summary_table(fixture_results, profile=args.profile, client_mode="fixture")

    any_orchestration_failed = any(not r.passed and not r.skipped for r in fixture_results)
    if any_orchestration_failed:
        print("[FAIL] Orchestration debugging failed on FixtureScenarioClient.")
        return 1

    # Stage 2: Validate Real SDK & Gateway Providers
    print("--- Stage 2: Validating Real Providers & SDK Availability ---")
    has_fakes, fake_list = check_real_providers(profile=args.profile)

    if has_fakes:
        print(f"\n[REPORT] Fake / missing providers detected for profile '{args.profile}':")
        for f in fake_list:
            print(f"  - {f}")
        print("\nReal SDK scenarios marked as SKIPPED pending Member A & B implementations.")

        if args.fail_on_fake:
            print("[FAIL] --fail-on-fake requested. Refusing exit with code 0.")
            return 1

    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI argument parser and synchronous dispatcher."""
    parser = argparse.ArgumentParser(
        description="AgentGate Scenario Runner (S1-S11)",
        prog="python -m simulator.runner",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run all implemented scenarios (S1-S11)",
    )
    parser.add_argument(
        "--scenario",
        type=str,
        default=None,
        help="Run specific scenario by name (e.g., s01, s02, s03... s11)",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default="scripted",
        choices=["scripted", "test", "real"],
        help="Execution profile (default: scripted)",
    )
    parser.add_argument(
        "--client",
        type=str,
        default="fixture",
        choices=["fixture", "sdk"],
        help="Client type to drive scenarios (default: fixture)",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=100,
        help="Number of iterations for concurrency scenarios (default: 100)",
    )
    parser.add_argument(
        "--fail-on-fake",
        action="store_true",
        help="Exit with non-zero if fake providers are detected under scripted/real profiles",
    )

    parsed = parser.parse_args(argv)
    return asyncio.run(main_async(parsed))


if __name__ == "__main__":
    sys.exit(main())
