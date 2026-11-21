"""Member B canonical vector and cryptographic primitive verifier."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contracts import CONTRACT_VERSION
from contracts.canonical import canonical_json, sha256_prefixed
from contracts.common import ActionType
from contracts.core_models import (
    ActionParams,
    CanonicalAction,
    Intent,
    MandateTerms,
)
from gateway.identity.crypto import (
    GENESIS_PREV_HASH,
    action_hash,
    audit_entry_hash,
    body_sha256_b64url,
    ed25519_verify_b64url,
    jwk_thumbprint,
    jwk_to_public_key,
    mandate_hash,
    request_fingerprint,
    verify_audit_entry_signature,
    verify_prefixed_sha256,
)

ROOT = Path(__file__).resolve().parents[1]


def _assert_eq(label: str, actual: Any, expected: Any) -> None:
    if actual != expected:
        raise SystemExit(f"{label} mismatch: expected {expected!r}, got {actual!r}")


def verify_frozen_action_vector() -> str:
    path = ROOT / "contracts/fixtures/b/action_hash_vector.json"
    vector = json.loads(path.read_text(encoding="utf-8"))
    actual = sha256_prefixed(canonical_json(vector["projection"]))
    _assert_eq("frozen action_hash_vector", actual, vector["expected_action_hash"])
    return actual


def verify_b01_golden_vectors() -> None:
    path = ROOT / "tests/support/b/golden_vectors.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    _assert_eq("golden_vectors contract_version", data.get("contract_version"), CONTRACT_VERSION)
    if data.get("secrets_present") is not False:
        raise SystemExit("golden_vectors.json must declare secrets_present=false")

    # 1. Action vectors (frozen, reordered, +1 paise, altered task, Unicode)
    actions = data["action_vectors"]
    frozen = actions["frozen_bill_pay"]
    base_expected = frozen["expected_action_hash"]
    _assert_eq(
        "golden frozen_bill_pay",
        sha256_prefixed(canonical_json(frozen["projection"])),
        base_expected,
    )

    reordered_obj = json.loads(actions["reordered_raw_json"])
    _assert_eq(
        "reordered semantic JSON",
        sha256_prefixed(canonical_json(reordered_obj)),
        base_expected,
    )

    base_action = CanonicalAction(
        request_id="req_vec_1",
        customer_id=frozen["projection"]["customer_id"],
        agent_id=frozen["projection"]["agent_id"],
        mandate_id=frozen["projection"]["mandate_id"],
        mandate_hash=frozen["projection"]["mandate_hash"],
        action_type=ActionType(frozen["projection"]["action_type"]),
        params=ActionParams(**frozen["projection"]["params"]),
        intent=Intent(**frozen["projection"]["intent"], note="Monthly bill"),
        idempotency_key=frozen["projection"]["idempotency_key"],
        received_at=datetime(2026, 11, 17, 4, 0, 0, tzinfo=UTC),
    )
    _assert_eq("CanonicalAction base action_hash", action_hash(base_action), base_expected)

    alt_amount = base_action.model_copy(
        update={
            "params": base_action.params.model_copy(
                update={"amount_paise": actions["altered_amount_plus_one_paise"]["amount_paise"]}
            )
        }
    )
    _assert_eq(
        "altered amount +1 paise",
        action_hash(alt_amount),
        actions["altered_amount_plus_one_paise"]["expected_action_hash"],
    )

    alt_task = base_action.model_copy(
        update={
            "intent": base_action.intent.model_copy(
                update={"task": actions["altered_task"]["task"]}
            )
        }
    )
    _assert_eq(
        "altered task",
        action_hash(alt_task),
        actions["altered_task"]["expected_action_hash"],
    )

    uni = actions["unicode_reference_and_note"]
    unicode_action = base_action.model_copy(
        update={
            "params": base_action.params.model_copy(update={"reference": uni["reference"]}),
            "intent": base_action.intent.model_copy(update={"note": uni["note"]}),
        }
    )
    _assert_eq("unicode action_hash", action_hash(unicode_action), uni["expected_action_hash"])

    # 2. Body hashes (bsh) and request fingerprints
    bodies = data["body_and_fingerprint_vectors"]
    _assert_eq("empty body bsh", body_sha256_b64url(b""), bodies["empty_body_bsh"])
    _assert_eq(
        "base canonical body bsh",
        body_sha256_b64url(canonical_json(bodies["base_body"])),
        bodies["base_canonical_body_bsh"],
    )
    _assert_eq(
        "base request fingerprint",
        request_fingerprint("POST", "/v1/payments/bills", bodies["base_body"]),
        bodies["base_request_fingerprint"],
    )
    _assert_eq(
        "note-changed canonical body bsh",
        body_sha256_b64url(canonical_json(bodies["note_changed_body"])),
        bodies["note_changed_canonical_body_bsh"],
    )
    _assert_eq(
        "note-changed request fingerprint",
        request_fingerprint("POST", "/v1/payments/bills", bodies["note_changed_body"]),
        bodies["note_changed_request_fingerprint"],
    )

    # 3. RFC 8037 Ed25519 public JWK, thumbprint, and signature
    rfc = data["rfc8037_ed25519"]
    pub_jwk = rfc["public_jwk"]
    _assert_eq("RFC 8037 jkt", jwk_thumbprint(pub_jwk), rfc["expected_jkt"])
    pub_key = jwk_to_public_key(pub_jwk)
    if not ed25519_verify_b64url(
        pub_key,
        rfc["signing_input_ascii"].encode("ascii"),
        rfc["expected_sig_b64url"],
    ):
        raise SystemExit("RFC 8037 Ed25519 signature verification failed")

    # 4. Mandate terms hash & signature
    mvec = data["mandate_vector"]
    terms = MandateTerms.model_validate(mvec["terms"])
    actual_mh = mandate_hash(terms)
    _assert_eq("mandate_hash", actual_mh, mvec["expected_mandate_hash"])
    if not verify_prefixed_sha256(pub_key, actual_mh, mvec["expected_sig_b64url"]):
        raise SystemExit("mandate signature verification failed")

    # 5. Audit chain hash & signature
    prev = GENESIS_PREV_HASH
    for idx, item in enumerate(data["audit_chain_vectors"], start=1):
        entry = item["entry"]
        _assert_eq(f"audit entry {idx} prev_hash", entry["prev_hash"], prev)
        computed = audit_entry_hash(prev, entry)
        _assert_eq(f"audit entry {idx} hash", computed, item["expected_entry_hash"])
        if not verify_audit_entry_signature(pub_key, computed, item["expected_sig_b64url"]):
            raise SystemExit(f"audit entry {idx} signature verification failed")
        prev = computed


def main() -> None:
    frozen_hash = verify_frozen_action_vector()
    verify_b01_golden_vectors()
    print(f"{frozen_hash} (all B01 golden vectors verified)")


if __name__ == "__main__":
    main()
