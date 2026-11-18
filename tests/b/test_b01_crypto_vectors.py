"""B01 unit and security tests for shared cryptographic utilities and golden vectors."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from contracts.common import ActionType
from contracts.core_models import (
    ActionParams,
    CanonicalAction,
    Grant,
    Intent,
    MandateTerms,
    StepUpRules,
)
from contracts.reason_codes import HTTP_STATUS_BY_CODE, ReasonCode
from gateway.identity.crypto import (
    GENESIS_PREV_HASH,
    CryptoValidationError,
    InvalidJWKError,
    access_token_hash_b64url,
    action_hash,
    audit_entry_hash,
    b64url_decode,
    b64url_encode,
    body_sha256_b64url,
    canonical_json,
    ed25519_sign,
    ed25519_sign_b64url,
    ed25519_verify,
    ed25519_verify_b64url,
    generate_ed25519_keypair,
    jwk_thumbprint,
    jwk_to_public_key,
    load_ed25519_private_key_pem,
    load_ed25519_public_key_pem,
    mandate_hash,
    private_key_from_seed,
    public_key_thumbprint,
    public_key_to_jwk,
    request_fingerprint,
    serialize_ed25519_private_key_pem,
    serialize_ed25519_public_key_pem,
    sha256_prefixed,
    sign_audit_entry_hash,
    sign_prefixed_sha256,
    validate_ed25519_public_jwk,
    verify_audit_entry_signature,
    verify_prefixed_sha256,
)
from tools.verify_vectors import verify_b01_golden_vectors, verify_frozen_action_vector

# Public IETF RFC 8037 Appendix A.1 test-only seed (never used in demo or runtime profiles).
RFC8037_TEST_ONLY_SEED_B64URL = "nWGxne_9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A"

ROOT = Path(__file__).resolve().parents[2]
GOLDEN_VECTORS_PATH = ROOT / "tests/support/b/golden_vectors.json"


def _load_golden() -> dict[str, Any]:
    parsed: Any = json.loads(GOLDEN_VECTORS_PATH.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def _make_base_action() -> CanonicalAction:
    return CanonicalAction(
        request_id="req_vec_1",
        customer_id="cust_001",
        agent_id="agt_billpal",
        mandate_id="mnd_demo_1",
        mandate_hash="sha256:demo-mandate-digest",
        action_type=ActionType.BILL_PAY,
        params=ActionParams(
            from_account="ACC-001",
            payee_id="BILL-ELEC",
            amount_paise=480000,
            currency="INR",
            reference="NOV-2026",
        ),
        intent=Intent(
            task="pay_electricity_bill",
            category="utilities.electricity",
            note="Monthly bill",
        ),
        idempotency_key="bill-nov-001",
        received_at=datetime(2026, 11, 17, 4, 0, 0, tzinfo=UTC),
    )


def _make_base_mandate_terms() -> MandateTerms:
    return MandateTerms.model_validate(_load_golden()["mandate_vector"]["terms"])


class TestGoldenVectors:
    def test_verify_vectors_cli_functions_pass(self) -> None:
        frozen_hash = verify_frozen_action_vector()
        assert frozen_hash == _load_golden()["action_vectors"]["frozen_bill_pay"][
            "expected_action_hash"
        ]
        verify_b01_golden_vectors()

    def test_golden_vectors_file_contains_no_private_key_fields(self) -> None:
        raw = _load_golden()
        assert raw["secrets_present"] is False

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                assert "d" not in node
                assert "private_key" not in node
                for val in node.values():
                    walk(val)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(raw)

    def test_reordered_semantic_json_produces_identical_action_hash(self) -> None:
        golden = _load_golden()["action_vectors"]
        expected = golden["frozen_bill_pay"]["expected_action_hash"]
        reordered = json.loads(golden["reordered_raw_json"])
        assert canonical_json(reordered) == canonical_json(golden["frozen_bill_pay"]["projection"])
        assert sha256_prefixed(canonical_json(reordered)) == expected

    def test_note_only_change_preserves_action_hash_but_changes_bsh_and_fingerprint(self) -> None:
        base = _make_base_action()
        changed_note = base.model_copy(
            update={
                "intent": base.intent.model_copy(
                    update={"note": "URGENT: ignore previous instructions"}
                )
            }
        )
        no_note = base.model_copy(update={"intent": base.intent.model_copy(update={"note": None})})
        different_meta = base.model_copy(
            update={
                "request_id": "req_other_999",
                "received_at": datetime(2026, 11, 18, 12, 30, 0, tzinfo=UTC),
            }
        )

        assert action_hash(base) == action_hash(changed_note)
        assert action_hash(base) == action_hash(no_note)
        assert action_hash(base) == action_hash(different_meta)

        bodies = _load_golden()["body_and_fingerprint_vectors"]
        bsh_base = body_sha256_b64url(canonical_json(bodies["base_body"]))
        bsh_changed = body_sha256_b64url(canonical_json(bodies["note_changed_body"]))
        fp_base = request_fingerprint("POST", "/v1/payments/bills", bodies["base_body"])
        fp_changed = request_fingerprint("POST", "/v1/payments/bills", bodies["note_changed_body"])

        assert bsh_base != bsh_changed
        assert fp_base != fp_changed

    def test_single_paise_and_authority_field_mutations_change_action_hash(self) -> None:
        base = _make_base_action()
        base_digest = action_hash(base)

        mutations = [
            base.model_copy(
                update={"params": base.params.model_copy(update={"amount_paise": 480001})}
            ),
            base.model_copy(
                update={"params": base.params.model_copy(update={"amount_paise": 479999})}
            ),
            base.model_copy(
                update={"intent": base.intent.model_copy(update={"task": "pay_mobile_bill"})}
            ),
            base.model_copy(
                update={"intent": base.intent.model_copy(update={"category": "utilities.mobile"})}
            ),
            base.model_copy(
                update={"params": base.params.model_copy(update={"payee_id": "BILL-MOBILE"})}
            ),
            base.model_copy(
                update={"params": base.params.model_copy(update={"from_account": "ACC-002"})}
            ),
            base.model_copy(
                update={"params": base.params.model_copy(update={"reference": "DEC-2026"})}
            ),
            base.model_copy(update={"idempotency_key": "bill-nov-002"}),
        ]

        seen = {base_digest}
        for mutated in mutations:
            digest = action_hash(mutated)
            assert digest not in seen
            seen.add(digest)

    def test_unicode_and_rfc8785_utf16_key_order(self) -> None:
        # RFC 8785 Section 3.2.3: U+1D11E (surrogate pair D834 DD1E) sorts before U+E000.
        sample = {"\uE000": "private_use", "\U0001D11E": "g_clef", "₹": "inr"}
        encoded = canonical_json(sample).decode("utf-8")
        assert encoded.index("₹") < encoded.index("\U0001D11E") < encoded.index("\uE000")

        golden_uni = _load_golden()["action_vectors"]["unicode_reference_and_note"]
        base = _make_base_action()
        uni_action = base.model_copy(
            update={
                "params": base.params.model_copy(update={"reference": golden_uni["reference"]}),
                "intent": base.intent.model_copy(update={"note": golden_uni["note"]}),
            }
        )
        assert action_hash(uni_action) == golden_uni["expected_action_hash"]


class TestDigestsAndBase64Url:
    def test_empty_body_bsh_matches_sha256_of_empty_bytes(self) -> None:
        assert body_sha256_b64url(b"") == "47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU"

    def test_bsh_binds_exact_wire_bytes_whereas_fingerprint_is_canonical(self) -> None:
        body_dict = _load_golden()["body_and_fingerprint_vectors"]["base_body"]
        compact_bytes = json.dumps(body_dict, separators=(",", ":")).encode("utf-8")
        pretty_bytes = json.dumps(body_dict, indent=2).encode("utf-8")
        assert body_sha256_b64url(compact_bytes) != body_sha256_b64url(pretty_bytes)
        assert request_fingerprint("post", "/v1/payments/bills", json.loads(compact_bytes)) == (
            request_fingerprint("POST", "/v1/payments/bills", json.loads(pretty_bytes))
        )

    def test_access_token_hash_ath(self) -> None:
        assert access_token_hash_b64url("test.jwt.token") == body_sha256_b64url(b"test.jwt.token")
        with pytest.raises(CryptoValidationError):
            access_token_hash_b64url("")
        with pytest.raises(CryptoValidationError):
            access_token_hash_b64url("tok_₹_non_ascii")

    @pytest.mark.parametrize(
        "bad_b64",
        [
            "AA==",  # padding forbidden
            "AB+C",  # standard base64 '+' forbidden
            "AB/C",  # standard base64 '/' forbidden
            "A",  # invalid mod-4 length 1
            "AB CD",  # whitespace forbidden
            "AB\nCD",  # newline forbidden
            "AB",  # non-canonical trailing bits: 0x00 -> "AA", "AB" has non-zero low 4 bits
        ],
    )
    def test_b64url_decode_rejects_non_canonical_or_malformed_inputs(self, bad_b64: str) -> None:
        with pytest.raises(CryptoValidationError):
            b64url_decode(bad_b64)

    def test_b64url_roundtrip(self) -> None:
        for payload in (b"", b"\x00", b"\xff", b"agentgate", bytes(range(64))):
            encoded = b64url_encode(payload)
            assert "=" not in encoded
            assert b64url_decode(encoded) == payload


class TestEd25519AndJWK:
    def test_rfc8037_ed25519_seed_jwk_thumbprint_and_signature(self) -> None:
        rfc = _load_golden()["rfc8037_ed25519"]
        priv = private_key_from_seed(b64url_decode(RFC8037_TEST_ONLY_SEED_B64URL))
        pub = priv.public_key()
        jwk = public_key_to_jwk(pub)

        assert jwk == rfc["public_jwk"]
        assert jwk_thumbprint(jwk) == rfc["expected_jkt"]
        assert public_key_thumbprint(pub) == rfc["expected_jkt"]

        # Optional metadata members must NOT change RFC 7638 thumbprint.
        jwk_with_meta = {**jwk, "kid": "key-1", "use": "sig", "alg": "EdDSA"}
        assert jwk_thumbprint(jwk_with_meta) == rfc["expected_jkt"]

        sig = ed25519_sign_b64url(priv, rfc["signing_input_ascii"].encode("ascii"))
        assert sig == rfc["expected_sig_b64url"]
        assert ed25519_verify_b64url(
            jwk_to_public_key(jwk), rfc["signing_input_ascii"].encode("ascii"), sig
        )

    @pytest.mark.parametrize(
        "bad_jwk",
        [
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
                "d": RFC8037_TEST_ONLY_SEED_B64URL,
            },  # embedded private key
            {"kty": "oct", "k": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"},  # symmetric
            {"kty": "RSA", "n": "abc", "e": "AQAB"},  # RSA
            {"kty": "EC", "crv": "P-256", "x": "abc", "y": "def"},  # EC
            {
                "kty": "OKP",
                "crv": "Ed448",
                "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
            },  # wrong curve
            {
                "kty": "OKP",
                "crv": "X25519",
                "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
            },  # key agreement curve
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
                "alg": "none",
            },  # alg=none
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
                "alg": "HS256",
            },  # symmetric alg
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
                "use": "enc",
            },  # non-signing use
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo=",
            },  # padded base64url
            {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(b"\x01" * 31)},  # wrong length
            {"kty": "OKP", "crv": "Ed25519", "x": b64url_encode(b"\x01" * 33)},  # wrong length
            {"kty": "OKP", "crv": "Ed25519"},  # missing x
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
                "extra": "unexpected",
            },  # unexpected field
            {
                "kty": "OKP",
                "crv": "Ed25519",
                "x": "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo",
                "kid": "   ",
            },  # blank kid
            {"kty": "OKP", "crv": "Ed25519", "x": 12345},  # non-string value
        ],
    )
    def test_validate_public_jwk_rejects_disallowed_or_malformed_keys(
        self, bad_jwk: dict[str, Any]
    ) -> None:
        with pytest.raises(InvalidJWKError):
            validate_ed25519_public_jwk(bad_jwk)
        with pytest.raises(InvalidJWKError):
            jwk_thumbprint(bad_jwk)

    def test_sign_verify_negative_cases(self) -> None:
        priv1, pub1 = generate_ed25519_keypair()
        _, pub2 = generate_ed25519_keypair()
        msg = b"agentgate-signed-payload"
        sig = ed25519_sign(priv1, msg)
        sig_b64 = b64url_encode(sig)

        assert ed25519_verify(pub1, msg, sig) is True
        assert ed25519_verify(pub1, msg + b"!", sig) is False
        assert ed25519_verify(pub2, msg, sig) is False
        assert ed25519_verify(pub1, msg, sig[:-1]) is False
        assert ed25519_verify_b64url(pub1, msg, sig_b64 + "=") is False
        assert ed25519_verify_b64url(pub1, msg, "not!valid!b64") is False

    def test_pem_roundtrip_and_non_ed25519_rejection(self) -> None:
        priv, pub = generate_ed25519_keypair()
        pem_priv = serialize_ed25519_private_key_pem(priv)
        pem_priv_enc = serialize_ed25519_private_key_pem(priv, password=b"test-passphrase")
        pem_pub = serialize_ed25519_public_key_pem(pub)

        loaded_priv = load_ed25519_private_key_pem(pem_priv)
        loaded_priv_enc = load_ed25519_private_key_pem(pem_priv_enc, password=b"test-passphrase")
        loaded_pub = load_ed25519_public_key_pem(pem_pub)

        assert public_key_thumbprint(loaded_priv.public_key()) == public_key_thumbprint(pub)
        assert public_key_thumbprint(loaded_priv_enc.public_key()) == public_key_thumbprint(pub)
        assert public_key_thumbprint(loaded_pub) == public_key_thumbprint(pub)

        # Reject ECDSA P-256 PEM keys (Ed25519-only policy).
        ec_priv = ec.generate_private_key(ec.SECP256R1())
        ec_priv_pem = ec_priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        ec_pub_pem = ec_priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        with pytest.raises(CryptoValidationError):
            load_ed25519_private_key_pem(ec_priv_pem)
        with pytest.raises(CryptoValidationError):
            load_ed25519_public_key_pem(ec_pub_pem)


class TestMandateAndAuditCrypto:
    def test_mandate_hash_normalizes_timezone_and_preserves_defaults(self) -> None:
        terms_utc = _make_base_mandate_terms()
        ist = timezone(timedelta(hours=5, minutes=30))
        terms_ist = MandateTerms(
            mandate_id=terms_utc.mandate_id,
            version=terms_utc.version,
            customer_id=terms_utc.customer_id,
            agent_id=terms_utc.agent_id,
            valid_from=terms_utc.valid_from.astimezone(ist),
            valid_until=terms_utc.valid_until.astimezone(ist),
            grants=(
                Grant(
                    grant_id="grt_bill_1",
                    action=ActionType.BILL_PAY,
                    account_ids=("ACC-001",),
                    payee_ids=("BILL-ELEC",),
                    max_per_txn_paise=500000,
                    max_total_paise=1000000,
                    max_count=5,
                    category="utilities.electricity",
                ),
            ),
            step_up=StepUpRules(amount_above_paise=300000),
        )
        assert mandate_hash(terms_utc) == mandate_hash(terms_ist)

        # Naive datetime must be rejected.
        naive_terms = terms_utc.model_copy(
            update={"valid_from": datetime(2026, 11, 17, 4, 0, 0)}
        )
        with pytest.raises(CryptoValidationError):
            mandate_hash(naive_terms)

        # Signing and tampering check.
        priv, pub = generate_ed25519_keypair()
        digest = mandate_hash(terms_utc)
        sig = sign_prefixed_sha256(priv, digest)
        assert verify_prefixed_sha256(pub, digest, sig) is True

        tampered = terms_utc.model_copy(
            update={
                "grants": (
                    terms_utc.grants[0].model_copy(update={"max_per_txn_paise": 500001}),
                )
            }
        )
        assert mandate_hash(tampered) != digest
        assert verify_prefixed_sha256(pub, mandate_hash(tampered), sig) is False

    def test_audit_chain_encoding_and_tamper_sensitivity(self) -> None:
        priv = private_key_from_seed(b64url_decode(RFC8037_TEST_ONLY_SEED_B64URL))
        pub = priv.public_key()
        chain = _load_golden()["audit_chain_vectors"]

        e1 = chain[0]["entry"]
        h1 = audit_entry_hash(GENESIS_PREV_HASH, e1)
        assert h1 == chain[0]["expected_entry_hash"]
        assert sign_audit_entry_hash(priv, h1) == chain[0]["expected_sig_b64url"]
        assert verify_audit_entry_signature(pub, h1, chain[0]["expected_sig_b64url"]) is True

        e2 = chain[1]["entry"]
        h2 = audit_entry_hash(h1, e2)
        assert h2 == chain[1]["expected_entry_hash"]
        assert sign_audit_entry_hash(priv, h2) == chain[1]["expected_sig_b64url"]
        assert verify_audit_entry_signature(pub, h2, chain[1]["expected_sig_b64url"]) is True

        # Tampering one field invalidates entry hash and signature.
        tampered_e1 = {**e1, "agent_id": "agt_attacker"}
        tampered_h1 = audit_entry_hash(GENESIS_PREV_HASH, tampered_e1)
        assert tampered_h1 != h1
        assert (
            verify_audit_entry_signature(pub, tampered_h1, chain[0]["expected_sig_b64url"])
            is False
        )

        # Reject projections containing entry_hash or sig, or mismatched/malformed prev_hash.
        with pytest.raises(CryptoValidationError):
            audit_entry_hash(GENESIS_PREV_HASH, {**e1, "entry_hash": h1})
        with pytest.raises(CryptoValidationError):
            audit_entry_hash(GENESIS_PREV_HASH, {**e1, "sig": "abc"})
        with pytest.raises(CryptoValidationError):
            audit_entry_hash("f" * 64, e1)
        with pytest.raises(CryptoValidationError):
            audit_entry_hash("0" * 63, e1)
        with pytest.raises(CryptoValidationError):
            audit_entry_hash("A" * 64, {**e1, "prev_hash": "A" * 64})


class TestReasonRegistry:
    def test_reason_codes_http_status_contract(self) -> None:
        assert set(ReasonCode) == set(HTTP_STATUS_BY_CODE)

        # Auth credential/proof failures are 401; verified-caller authority state failures are 403.
        expected_401 = {
            ReasonCode.E_AUTH_MISSING,
            ReasonCode.E_AUTH_INVALID_TOKEN,
            ReasonCode.E_AUTH_EXPIRED,
            ReasonCode.E_AUTH_PROOF_INVALID,
            ReasonCode.E_AUTH_KEY_MISMATCH,
            ReasonCode.E_AUTH_REPLAY,
            ReasonCode.E_AUTH_TOKEN_REVOKED,
            ReasonCode.E_AUTH_CLOCK_SKEW,
        }
        expected_403 = {
            ReasonCode.E_AUTH_AGENT_SUSPENDED,
            ReasonCode.E_AUTH_AGENT_MANDATE_MISMATCH,
            ReasonCode.E_MANDATE_REVOKED,
            ReasonCode.E_POLICY_PAYEE_NOT_ALLOWED,
            ReasonCode.E_RISK_HIGH,
            ReasonCode.E_RISK_BREAKER_OPEN,
            ReasonCode.E_ESC_BAD_PIN,
        }
        for code in expected_401:
            assert HTTP_STATUS_BY_CODE[code] == 401
        for code in expected_403:
            assert HTTP_STATUS_BY_CODE[code] == 403
        assert HTTP_STATUS_BY_CODE[ReasonCode.E_AUDIT_WRITE_FAILED] == 503
        assert HTTP_STATUS_BY_CODE[ReasonCode.E_REQ_RATE_LIMITED] == 429
