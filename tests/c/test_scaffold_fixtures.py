import json
from pathlib import Path

import pytest

from contracts import CONTRACT_VERSION

pytestmark = [pytest.mark.scaffold, pytest.mark.contract]


def test_risk_and_approval_fixture_is_versioned() -> None:
    root = Path(__file__).resolve().parents[2]
    fixture = json.loads(
        (root / "contracts/fixtures/c/risk_approval_cases.json").read_text(encoding="utf-8")
    )
    assert fixture["contract_version"] == CONTRACT_VERSION
    assert {case["recommended"] for case in fixture["risk_cases"]} == {
        "ALLOW",
        "ESCALATE",
        "DENY",
    }
