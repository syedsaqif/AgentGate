import json
from pathlib import Path

import pytest

from contracts.canonical import canonical_json, sha256_prefixed
from contracts.reason_codes import HTTP_STATUS_BY_CODE, ReasonCode

pytestmark = [pytest.mark.scaffold, pytest.mark.contract]


def test_frozen_action_hash_vector() -> None:
    root = Path(__file__).resolve().parents[2]
    vector = json.loads(
        (root / "contracts/fixtures/b/action_hash_vector.json").read_text(encoding="utf-8")
    )
    assert sha256_prefixed(canonical_json(vector["projection"])) == vector["expected_action_hash"]


def test_reason_codes_have_http_mappings() -> None:
    assert set(ReasonCode) == set(HTTP_STATUS_BY_CODE)
