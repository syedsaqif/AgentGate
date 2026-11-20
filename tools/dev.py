"""Member A cross-platform developer CLI and service launcher."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn
import yaml

from contracts import CONTRACT_VERSION
from contracts.canonical import canonical_json, sha256_prefixed
from gateway.config import AgentGateConfig
from gateway.platform.dev import DEFAULT_DEV_KEY

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_PATHS = (
    "contracts/common.py",
    "contracts/core_models.py",
    "contracts/identity_models.py",
    "contracts/audit_models.py",
    "contracts/risk_models.py",
    "contracts/escalation_models.py",
    "contracts/openapi.json",
    "gateway/main.py",
    "gateway/platform",
    "gateway/mandates",
    "gateway/executor",
    "gateway/events",
    "gateway/identity",
    "gateway/audit",
    "gateway/risk",
    "gateway/escalation",
    "bank_mock",
    "agent_sdk",
    "simulator/scenarios/s11.py",
    "frontend/src/api",
    "frontend/src/mocks",
    "frontend/src/pages/customer",
    "frontend/src/pages/ops",
    "tests/a",
    "tests/b",
    "tests/c",
    "docs/a",
    "docs/b",
    "docs/c",
    "docs/d",
)

OWNER_FIXTURES = {
    "a": "contracts/fixtures/a/policy_cases.json",
    "b": "contracts/fixtures/b/auth_audit_cases.json",
    "c": "contracts/fixtures/c/risk_approval_cases.json",
    "d": "contracts/fixtures/d/ui_states.json",
}


def _load_json(relative: str) -> dict[str, Any]:
    with (ROOT / relative).open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{relative} must contain a JSON object")
    return value


def check_contracts(owner: str | None = None) -> None:
    missing = [relative for relative in REQUIRED_PATHS if not (ROOT / relative).exists()]
    if missing:
        raise SystemExit(f"Missing required scaffold paths: {', '.join(missing)}")

    openapi = _load_json("contracts/openapi.json")
    if openapi.get("info", {}).get("version") != CONTRACT_VERSION:
        raise SystemExit("OpenAPI contract version does not match contracts.CONTRACT_VERSION")
    if len(openapi.get("paths", {})) != 27:
        raise SystemExit("Contract-only OpenAPI path inventory drifted from the frozen plan")

    selected = OWNER_FIXTURES if owner is None else {owner: OWNER_FIXTURES[owner]}
    for relative in selected.values():
        fixture = _load_json(relative)
        if fixture.get("contract_version") != CONTRACT_VERSION:
            raise SystemExit(f"Contract version mismatch in {relative}")

    for relative in ("contracts/http/a.yaml", "contracts/http/b.yaml", "contracts/http/c.yaml"):
        with (ROOT / relative).open(encoding="utf-8") as stream:
            fragment = yaml.safe_load(stream)
        if fragment.get("contract_version") != CONTRACT_VERSION:
            raise SystemExit(f"Contract version mismatch in {relative}")

    vector = _load_json("contracts/fixtures/b/action_hash_vector.json")
    actual = sha256_prefixed(canonical_json(vector["projection"]))
    if actual != vector["expected_action_hash"]:
        raise SystemExit("Frozen action hash vector does not match RFC 8785 canonical bytes")

    scope = f"owner {owner.upper()}" if owner else "all owners"
    print(f"AgentGate {CONTRACT_VERSION}: contract/scaffold checks passed for {scope}.")
    print("This is an IC0 check only; it does not claim feature or security implementation.")


def run_service(owner: str, profile: str, host: str = "127.0.0.1", port: int = 8000) -> None:
    """Launch the appropriate application service for local track development."""
    os.environ["AGENTGATE_PROFILE"] = profile
    if owner == "a":
        os.environ.setdefault("AGENTGATE_GATEWAY_ORIGIN", f"http://{host}:{port}")
        print(f"Starting Track A Gateway on http://{host}:{port} with profile '{profile}'...")
        uvicorn.run("gateway.main:app", host=host, port=port, log_level="info")
    else:
        print(f"Service for owner '{owner}' is reserved for that track's roadmap tasks.")


def run_stack(
    profile: str, host: str = "127.0.0.1", port: int = 8000, bank_port: int = 8001
) -> None:
    """Start the simulated bank first, then the gateway using the same local configuration."""
    os.environ["AGENTGATE_PROFILE"] = profile
    os.environ["AGENTGATE_GATEWAY_ORIGIN"] = f"http://{host}:{port}"
    os.environ["AGENTGATE_BANK_ORIGIN"] = f"http://127.0.0.1:{bank_port}"
    config = AgentGateConfig.load(profile=profile)
    if profile == "real" and not config.bank_service_credential:
        raise SystemExit("Set AGENTGATE_BANK_SERVICE_CREDENTIAL before starting the real stack.")

    print(f"Gateway readiness ({profile}): {config.gateway_origin}/health/ready", flush=True)
    bank_cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "bank_mock.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(bank_port),
        "--log-level",
        "info",
    ]
    bank_proc = subprocess.Popen(bank_cmd)
    try:
        with httpx.Client(timeout=0.5, trust_env=False) as client:
            for _ in range(80):
                if bank_proc.poll() is not None:
                    raise SystemExit("The simulated bank exited during startup.")
                try:
                    ready = client.get(f"{config.bank_origin}/openapi.json")
                    if ready.is_success and ready.json().get("info", {}).get("title") == (
                        "AgentGate Mock Bank"
                    ):
                        break
                except (httpx.TransportError, ValueError):
                    pass
                time.sleep(0.125)
            else:
                raise SystemExit("The simulated bank did not become ready.")
        uvicorn.run("gateway.main:app", host=host, port=port, log_level="info")
    finally:
        if bank_proc.poll() is None:
            bank_proc.terminate()
        bank_proc.wait()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m tools.dev")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check-contracts")
    check.add_argument("--owner", choices=tuple(OWNER_FIXTURES))

    service = subparsers.add_parser("service")
    service.add_argument("owner", choices=("a", "b", "c", "d"))
    service.add_argument("--profile", default="test", choices=("test", "scripted", "real"))
    service.add_argument("--host", default="127.0.0.1")
    service.add_argument("--port", type=int, default=8000)

    reset = subparsers.add_parser("reset")
    reset.add_argument(
        "--profile", default="scripted", choices=("test", "scripted", "real")
    )
    reset.add_argument("--base-url", default="http://127.0.0.1:8000")

    stack = subparsers.add_parser("stack")
    stack.add_argument(
        "--profile", default="real", choices=("test", "scripted", "real")
    )
    stack.add_argument("--host", default="127.0.0.1")
    stack.add_argument("--port", type=int, default=8000)
    stack.add_argument("--bank-port", type=int, default=8001)

    args = parser.parse_args()
    if args.command == "check-contracts":
        check_contracts(args.owner)
    elif args.command == "service":
        run_service(owner=args.owner, profile=args.profile, host=args.host, port=args.port)
    elif args.command == "reset":
        from tools.reset import reset_dev

        asyncio.run(reset_dev(args.base_url, DEFAULT_DEV_KEY, args.profile))
    elif args.command == "stack":
        run_stack(
            profile=args.profile,
            host=args.host,
            port=args.port,
            bank_port=args.bank_port,
        )


if __name__ == "__main__":
    main()
