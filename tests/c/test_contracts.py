"""Unit tests validating contracts and fixtures for Member C."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from contracts.common import FinalDecision, RiskLevel
from contracts.escalation_models import EscalationDecision, EscalationState

ROOT = Path(__file__).resolve().parents[2]


def test_escalation_states_match_contract() -> None:
    with (ROOT / "contracts/http/c.yaml").open(encoding="utf-8") as stream:
        c_yaml = yaml.safe_load(stream)
    yaml_states = set(c_yaml["approval"]["states"])
    enum_states = {state.value for state in EscalationState}
    assert enum_states == yaml_states


def test_escalation_decisions_match_contract() -> None:
    with (ROOT / "contracts/http/c.yaml").open(encoding="utf-8") as stream:
        c_yaml = yaml.safe_load(stream)
    yaml_decisions = set(c_yaml["approval"]["decisions"])
    enum_decisions = {decision.value for decision in EscalationDecision}
    assert enum_decisions == yaml_decisions


def test_risk_cases_map_to_valid_enums() -> None:
    with (ROOT / "contracts/fixtures/c/risk_approval_cases.json").open(encoding="utf-8") as stream:
        fixture = json.load(stream)
    valid_risk_levels = {level.value for level in RiskLevel}
    valid_final_decisions = {decision.value for decision in FinalDecision}

    for case in fixture["risk_cases"]:
        assert case["level"] in valid_risk_levels
        assert case["recommended"] in valid_final_decisions


def test_approval_terminal_states_are_valid_escalation_states() -> None:
    with (ROOT / "contracts/fixtures/c/risk_approval_cases.json").open(encoding="utf-8") as stream:
        fixture = json.load(stream)
    valid_escalation_states = {state.value for state in EscalationState}

    for state in fixture["approval_terminal_states"]:
        assert state in valid_escalation_states
