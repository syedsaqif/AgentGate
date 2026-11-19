"""Member A cross-platform developer CLI and service launcher."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import uvicorn
import yaml

from contracts import CONTRACT_VERSION
from contracts.canonical import canonical_json, sha256_prefixed

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
        print(f"Starting Track A Gateway on http://{host}:{port} with profile '{profile}'...")
        uvicorn.run("gateway.main:app", host=host, port=port, log_level="info")
    else:
        print(f"Service for owner '{owner}' is reserved for that track's roadmap tasks.")


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

    args = parser.parse_args()
    if args.command == "check-contracts":
        check_contracts(args.owner)
    elif args.command == "service":
        run_service(owner=args.owner, profile=args.profile, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
