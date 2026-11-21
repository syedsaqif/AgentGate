# Member B Handoff Notes — Identity & Audit

This document tracks Member B's implementation across stages B01–B08, including shared cryptographic conventions, proof/hash vectors, authentication domains, audit transaction semantics, and safe key management.

## Stage Status

| Stage | Scope | Status |
|---|---|---|
| B01 | Shared cryptographic utilities and golden vectors | Completed |
| B02 | Agent registry and human authentication | Completed |
| B03 | DPoP-style proof verification and replay protection | Completed |
| B04 | Token lifecycle and revocation | Completed |
| B05 | Python Agent SDK | Completed |
| B06 | Persistent signed audit chain | Completed |
| B07 | Verification, checkpoints and evidence generation | Completed |
| B08 | Integration and security testing | Completed (Member B real providers + SDK; awaiting Member A/C real gateway assembly) |

---

## B01 — Shared Cryptographic Utilities & Golden Vectors

### Implemented Modules & Fixtures

- [`gateway/identity/crypto.py`](../../gateway/identity/crypto.py) — Shared cryptographic primitives using reviewed libraries (`cryptography` and `rfc8785`), re-exporting frozen canonical functions from [`contracts/canonical.py`](../../contracts/canonical.py).
- [`tests/support/b/golden_vectors.json`](../../tests/support/b/golden_vectors.json) — Test-only public golden vectors (`secrets_present: false`, no private keys).
- [`tools/verify_vectors.py`](../../tools/verify_vectors.py) — Verifies the frozen `contracts/fixtures/b/action_hash_vector.json` and all B01 golden vectors.
- [`tests/b/test_b01_crypto_vectors.py`](../../tests/b/test_b01_crypto_vectors.py) — Unit and security tests for canonical projections, base64url strictness, Ed25519 JWK validation, RFC 7638 thumbprints, mandate hashing, audit chaining, and reason-code HTTP mappings.

### Three Distinct Hashes

| Helper | Input | Output Format | Purpose |
|---|---|---|---|
| `body_sha256_b64url(raw_body)` | Exact transmitted UTF-8 HTTP body bytes (`b""` for bodyless GET) | Unpadded base64url SHA-256 | DPoP `bsh` transit-integrity binding |
| `request_fingerprint(method, path, body)` | RFC 8785 JCS of `{method, path, body}` (includes `intent.note`) | `sha256:<64-hex>` | Idempotency-key body-conflict detection |
| `action_hash(action)` (from `contracts.canonical`) | RFC 8785 JCS of frozen authority projection (excludes `intent.note`, `request_id`, `received_at`) | `sha256:<64-hex>` | Approval and execution authority binding |

Additional canonical hashes:
- `mandate_hash(terms)` — `sha256:<64-hex>` over RFC 8785 JCS of `MandateTerms` with explicit defaults preserved and timestamps normalized to UTC ISO-8601 ending in `Z`.
- `audit_entry_hash(prev_hash, entry)` — `hex(SHA256(bytes.fromhex(prev_hash) || JCS(entry)))` with `GENESIS_PREV_HASH = "0" * 64`; entry must include `prev_hash` and exclude `entry_hash` and `sig`.
- `access_token_hash_b64url(token)` — RFC 9449 `ath` unpadded base64url SHA-256 of the ASCII access token string.

---

## B02 — Agent Registry & Human Authentication

### Implemented Modules

- [`gateway/identity/errors.py`](../../gateway/identity/errors.py) — Domain exception hierarchy mapped to frozen `ReasonCode` / `HTTP_STATUS_BY_CODE` values.
- [`gateway/identity/models.py`](../../gateway/identity/models.py) & [`gateway/identity/schema.py`](../../gateway/identity/schema.py) — SQLAlchemy 2 ORM models (`customers`, `operators`, `human_sessions`, `agents`, `proof_replays`), SQLite WAL + `BEGIN IMMEDIATE` setup, and caller-owned `Tx` transaction participation (never committing caller transactions).
- [`gateway/identity/registry.py`](../../gateway/identity/registry.py) — `SqlAgentRegistry` implementing `contracts.identity_ports.AgentRegistry` with strict Ed25519 public JWK validation, DB-enforced unique RFC 7638 `jkt`, bounded cursor pagination, and `agent.registered` audit emission.
- [`gateway/identity/human_auth.py`](../../gateway/identity/human_auth.py) — `SqlHumanAuthService` implementing `contracts.identity_ports.HumanAuth` with Argon2id PIN/password verification, constant-time unknown-user mitigation, role-isolated Ed25519 signing keys & JWT audiences (`agentgate-customer-session`, `agentgate-ops-session`, `agentgate-registrar-session`), DB-hashed session tracking, clock-driven failure throttling (`AuthThrottler`), and IDOR ownership helper `assert_customer_owns`.
- [`gateway/identity/routes.py`](../../gateway/identity/routes.py) — FastAPI router (`build_identity_router`) and frozen `ErrorEnvelope` handlers (`install_identity_error_handlers`) for `/v1/customer/login`, `/v1/ops/login`, `/v1/agents/register`, `/v1/ops/agents`, `/v1/ops/agents/{id}/suspend`, and `/v1/ops/agents/{id}/reinstate`.
- [`tests/support/b/providers.py`](../../tests/support/b/providers.py) — Explicit neighbor substitutes (`FixtureIdentityVerifier`, `FixtureHumanAuth`, `FixtureAgentRegistry`, `MemoryAuditLog`).
- [`tests/b/test_b02_registry_human_auth.py`](../../tests/b/test_b02_registry_human_auth.py) — Unit, security, transaction, and HTTP route tests.

---

## B03 — Agent Proof Verifier & Atomic Replay Protection

### Implemented Modules

- [`gateway/identity/replay.py`](../../gateway/identity/replay.py) — `SqlProofReplayCache` persisting `(registered_jkt, proof_jti)` in SQLite `proof_replays` with composite primary key, `BEGIN IMMEDIATE`, and `ON CONFLICT DO NOTHING`. Entries are retained through `max(iat + tolerance, now + tolerance)` so future-dated proofs stay protected across their entire acceptance window.
- [`gateway/identity/proofs.py`](../../gateway/identity/proofs.py) — `DPoPProofVerifier`, `VerifiedProof`, `build_trusted_htu`, `extract_dpop_header`.
- [`tests/b/test_b03_proof_verifier.py`](../../tests/b/test_b03_proof_verifier.py) — Unit, security, concurrent-thread replay atomicity, and risk-history isolation tests.

### Proof Format (AgentGate DPoP-Style Profile)

Header:
```json
{
  "typ": "dpop+jwt",
  "alg": "EdDSA",
  "jwk": {
    "kty": "OKP",
    "crv": "Ed25519",
    "x": "<32-byte-unpadded-base64url-public-key>"
  }
}
```

Payload claims:
- `htm`: Uppercase HTTP method matching the actual request method (`POST`, `GET`).
- `htu`: Authoritative target URI `"{trusted_origin}{route_path}"` with **no query string and no fragment** (spoofed `Host` / `X-Forwarded-*` / `Forwarded` headers are ignored).
- `iat`: Integer Unix timestamp within `±60` seconds of the injected `Clock.now()`.
- `jti`: Unique non-empty proof identifier (max 128 chars, no whitespace).
- `bsh`: Unpadded base64url SHA-256 of the exact transmitted UTF-8 body bytes (`47DEQpj8HBSa-_TImW-5JCeuQeRkm5NMpJWZG3hSuFU` for bodyless `GET`).
- `ath`: Required for resource requests (unpadded base64url SHA-256 of the raw ASCII access-token string); **forbidden** on token requests (`POST /v1/oauth/token`).

### Strict Validation Order

1. Presence and compact 3-part JWS structure (`E_AUTH_MISSING` if absent, `E_AUTH_PROOF_INVALID` if malformed).
2. Header checks: exact `typ="dpop+jwt"`, exact `alg="EdDSA"` (rejects `none`, `HS256`, `ES256`, `RS256`), strict Ed25519 public `jwk` validation (rejects embedded private parameters like `d`).
3. Optional `expected_jkt` check against token-bound thumbprint (`E_AUTH_KEY_MISMATCH`).
4. Registered agent resolution strictly by RFC 7638 `jkt` from `AgentRegistry` (request-body agent IDs are never trusted; unknown key returns the same public `401 E_AUTH_PROOF_INVALID` message as invalid signature while recording precise unattributed internal audit reason).
5. Ed25519 signature verification using the registered agent's public key (runs **before** any replay-cache mutation so forged proofs cannot DoS legitimate `jti` values).
6. Payload claim verification: `htm`, trusted-origin `htu`, `bsh`, `ath` (required vs forbidden), `iat` ±60s (`E_AUTH_CLOCK_SKEW`), and `jti`.
7. Atomic SQLite replay claim on `(registered_jkt, proof_jti)` (`E_AUTH_REPLAY` on duplicate or concurrent loser).
8. Returns `VerifiedProof` only after all checks succeed. Invalid proofs never reach verified risk-history recording, and raw proofs/tokens/private keys are never written to audit payloads.

### Proof Verifier Usage Example

```python
from gateway.identity.proofs import DPoPProofVerifier, extract_dpop_header
from gateway.identity.replay import SqlProofReplayCache

replay_cache = SqlProofReplayCache(session_factory, clock)
verifier = DPoPProofVerifier(
    registry=registry,
    replay_cache=replay_cache,
    clock=clock,
    trusted_origin="http://127.0.0.1:8000",
    audit_log=audit_log,
)

verified = await verifier.verify_proof(
    proof_jwt=extract_dpop_header(request.headers),
    method=request.method,
    request_url=str(request.url),
    request_bytes=raw_body_bytes,
    access_token=raw_access_token,  # None for POST /v1/oauth/token
    expected_jkt=token_cnf_jkt,     # None for POST /v1/oauth/token
)
```

---

## B04 — Agent Token Lifecycle & Revocation

### Implemented Modules

- [`gateway/identity/models.py`](../../gateway/identity/models.py) — Added `IssuedTokenRecord` (`issued_tokens` table) storing SHA-256 token digests and metadata (never raw access tokens).
- [`gateway/identity/tokens.py`](../../gateway/identity/tokens.py) — `SqlTokenService` (implements `contracts.identity_ports.TokenService` plus `issue_token` and `revoke_token`) and `SqlIdentityVerifier` (implements `contracts.identity_ports.IdentityVerifier`).
- [`gateway/identity/routes.py`](../../gateway/identity/routes.py) — Wired `POST /v1/oauth/token` reading exact raw request bytes before JSON validation and returning `200 AccessTokenResult` with `Cache-Control: no-store`.
- [`tests/support/b/providers.py`](../../tests/support/b/providers.py) — Added `FixtureMandateReader` implementing `contracts.core_ports.MandateReader`.
- [`tests/b/test_b04_token_lifecycle.py`](../../tests/b/test_b04_token_lifecycle.py) — Unit, security, revocation-precedence, and HTTP route tests.

### Agent Access Token Format

Header:
```json
{
  "alg": "EdDSA",
  "typ": "at+jwt",
  "kid": "<token-signing-public-jwk-thumbprint>"
}
```

Payload claims:
```json
{
  "iss": "agentgate",
  "aud": "agentgate-agent-api",
  "sub": "cust_001",
  "act": {"sub": "agt_billpal"},
  "mandate_id": "mnd_demo_1",
  "mandate_hash": "sha256:<mandate-terms-digest>",
  "cnf": {"jkt": "<registered-agent-public-jwk-thumbprint>"},
  "iat": 1795010400,
  "exp": 1795011000,
  "jti": "tok_<random-hex>"
}
```

- **TTL cap:** `min(default_ttl, 600, remaining_mandate_seconds)`. A token never exceeds 600 seconds and never outlives `mandate.terms.valid_until`.
- **Key isolation:** Agent tokens use a dedicated Ed25519 signing key and `aud="agentgate-agent-api"` / `typ="at+jwt"`, strictly rejected by `SqlHumanAuthService` and all human routes.
- **Revocation ports:** `revoke_for_mandate(mandate_id, tx)` and `revoke_for_agent(agent_id, tx)` participate in caller-owned `Tx` transactions without committing `tx`. Reinstating a suspended agent never revives previously revoked tokens.

### Verification Precedence & Error Mappings

| Condition | HTTP | ReasonCode |
|---|---:|---|
| Missing `Authorization` or `DPoP` header | 401 | `E_AUTH_MISSING` |
| Wrong scheme, unknown signing key, wrong `alg`/`typ`/`iss`/`aud`, unissued token | 401 | `E_AUTH_INVALID_TOKEN` |
| Expired token or expired mandate | 401 | `E_AUTH_EXPIRED` |
| Stolen token used with different proof key (`cnf.jkt` mismatch) or `client_id` mismatch | 401 | `E_AUTH_KEY_MISMATCH` |
| Invalid proof header/signature/`htm`/`htu`/`bsh`/`ath`/`jti` or unknown agent key | 401 | `E_AUTH_PROOF_INVALID` |
| Proof timestamp outside ±60s skew window | 401 | `E_AUTH_CLOCK_SKEW` |
| Replayed `(jkt, proof_jti)` | 401 | `E_AUTH_REPLAY` |
| Live mandate revoked or suspended (checked before token revocation) | 403 | `E_MANDATE_REVOKED` |
| Live mandate wrong agent / wrong customer / wrong hash / not yet valid / missing | 403 | `E_AUTH_AGENT_MANDATE_MISMATCH` |
| Live agent suspended or disabled (checked before token revocation) | 403 | `E_AUTH_AGENT_SUSPENDED` |
| Token revoked while mandate and agent are otherwise active | 401 | `E_AUTH_TOKEN_REVOKED` |

---

## B05 — Python Agent SDK

### Implemented Modules

- [`agent_sdk/keys.py`](../../agent_sdk/keys.py) — Ed25519 key generation (`generate_agent_keypair`), PKCS8 PEM save/load (`save_private_key_pem`, `load_private_key_pem` with overwrite protection and Ed25519-only enforcement), public JWK export (`save_public_jwk`), and RFC 7638 `jkt`.
- [`agent_sdk/serialization.py`](../../agent_sdk/serialization.py) — Serialize-once RFC 8785 UTF-8 body (`SerializedRequestBody`, `serialize_json_body`), strict `MoneyActionPayload` (integer `PositivePaise`, `INR` only), and `new_idempotency_key`.
- [`agent_sdk/proofs.py`](../../agent_sdk/proofs.py) — DPoP-style Ed25519 proof builder (`build_dpop_proof`, `build_htu` stripping query/fragment).
- [`agent_sdk/tokens.py`](../../agent_sdk/tokens.py) — `TokenManager` fetching and refreshing short-lived DPoP tokens from `POST /v1/oauth/token` (never bypassing revoked, suspended, or expired authority) and structured `AgentAPIError`.
- [`agent_sdk/retry.py`](../../agent_sdk/retry.py) — `RetryPolicy` and `execute_with_fresh_proof_retry`, guaranteeing fresh proof `jti` on every attempt while keeping exact wire bytes and `Idempotency-Key` immutable.
- [`agent_sdk/polling.py`](../../agent_sdk/polling.py) — `poll_escalation` and `poll_action` helpers handling `PENDING`/`APPROVED`/`EXECUTED`/`REJECTED`/`EXPIRED`/`CANCELLED`/`FAILED`/`EXECUTION_UNKNOWN`.
- [`agent_sdk/client.py`](../../agent_sdk/client.py) — High-level async `AgentGateClient` with `fetch_token`, `get_balance`, `list_transactions`, `pay_bill`, `transfer`, `get_action`, `get_escalation`, `poll_action`, and `poll_escalation`.
- [`agent_sdk/cli.py`](../../agent_sdk/cli.py) — CLI (`python -m agent_sdk.cli`) for `keygen`, `inspect-key`, `balance`, `pay-bill`, `poll-escalation`, and `poll-action`.
- [`agent_sdk/__init__.py`](../../agent_sdk/__init__.py) — Public SDK package exports (zero dependency on `gateway.*`).
- [`tests/b/test_b05_agent_sdk.py`](../../tests/b/test_b05_agent_sdk.py) — `httpx.MockTransport` security tests, CLI tests, and end-to-end SDK ↔ B02/B03/B04 gateway verifier interop test.

### Runnable Python Example

```python
import asyncio
from pathlib import Path

from agent_sdk import (
    AgentGateClient,
    generate_agent_keypair,
    load_private_key_pem,
    save_private_key_pem,
)

async def main() -> None:
    key_path = Path("runtime/agent_billpal.pem")
    if not key_path.exists():
        save_private_key_pem(generate_agent_keypair(), key_path)
    keypair = load_private_key_pem(key_path)

    async with AgentGateClient(
        gateway_url="http://127.0.0.1:8000",
        agent_id="agt_billpal",
        mandate_id="mnd_demo_1",
        keypair=keypair,
    ) as client:
        result = await client.pay_bill(
            from_account="ACC-001",
            payee_id="BILL-ELEC",
            amount_paise=185000,
            reference="NOV-2026",
            task="pay_electricity_bill",
            category="utilities.electricity",
            note="Monthly electricity bill",
            idempotency_key="bill-nov-001",
        )
        if result.get("escalation_id"):
            final = await client.poll_escalation(result["escalation_id"])
            print(final.status, final.payload)
        else:
            print(result)

asyncio.run(main())
```

### CLI Examples

```text
# Generate an Ed25519 agent keypair outside source control (prints public JWK + jkt only)
uv run python -m agent_sdk.cli keygen --out runtime/agent_billpal.pem --pub-out runtime/agent_billpal.pub.json

# Inspect public JWK and thumbprint
uv run python -m agent_sdk.cli inspect-key --key runtime/agent_billpal.pem

# Signed balance read (bodyless GET, empty-byte bsh)
uv run python -m agent_sdk.cli balance \
  --gateway-url http://127.0.0.1:8000 \
  --agent-id agt_billpal \
  --mandate-id mnd_demo_1 \
  --key runtime/agent_billpal.pem \
  --account-id ACC-001

# Signed bill payment with integer paise and automatic approval polling
uv run python -m agent_sdk.cli pay-bill \
  --gateway-url http://127.0.0.1:8000 \
  --agent-id agt_billpal \
  --mandate-id mnd_demo_1 \
  --key runtime/agent_billpal.pem \
  --from-account ACC-001 \
  --payee-id BILL-ELEC \
  --amount-paise 480000 \
  --task pay_electricity_bill \
  --category utilities.electricity \
  --idempotency-key bill-nov-001 \
  --poll
```

---

## B06 — Tamper-Evident Audit Store

### Implemented Modules

- [`gateway/audit/models.py`](../../gateway/audit/models.py) — Private SQLAlchemy 2 `AuditLogRecord` (`audit_log` table in the shared gateway SQLite WAL database; no separate audit database).
- [`gateway/audit/schema.py`](../../gateway/audit/schema.py) — `register_audit_tables`, `install_audit_triggers`, and `create_audit_schema` installing SQLite `BEFORE UPDATE` and `BEFORE DELETE` `RAISE(ABORT, ...)` triggers.
- [`gateway/audit/checkpoints.py`](../../gateway/audit/checkpoints.py) — `SignedCheckpoint` and append-only `CheckpointStore` (`flush` + `fsync`, Ed25519-signed checkpoints).
- [`gateway/audit/verify.py`](../../gateway/audit/verify.py) — `verify_audit_chain` verifying contiguous `1..N` sequences, `prev_hash` links, recomputed `entry_hash`, Ed25519 signatures, and external checkpoint/anchor coverage.
- [`gateway/audit/append.py`](../../gateway/audit/append.py) — `SqlAuditLog` (conforming to `contracts.audit_ports.AuditLog`), `AuditWriteError` (`503 E_AUDIT_WRITE_FAILED`), recursive secret redaction (`sanitize_audit_value`), caller `Tx` participation, and serialized concurrent writes.
- [`tests/b/test_b06_audit_store.py`](../../tests/b/test_b06_audit_store.py) — Unit, trigger, tamper-detection (edit, middle delete, re-hash without signing key, anchored tail truncation), 16-thread concurrency, rollback, failure propagation, and secret-redaction tests.

### Audit Event Schema & Chain Encoding

Each row in `audit_log` stores:
- `seq` (strictly monotonic `1, 2, ...`), `prev_hash` (genesis `"0" * 64`), `event_type`, `request_id`, `correlation_id`, `customer_id`, `agent_id`, `mandate_id`, `mandate_hash`, `decision`, `reason_codes_json`, `risk_json`, `safe_payload_json`, `occurred_at` (UTC ISO-8601 `Z`), `entry_hash`, and `sig`.

Canonical hash & signature:
```text
entry_hash = hex(SHA256(bytes.fromhex(prev_hash) || JCS(entry_without_hash_and_sig)))
sig        = base64url(Ed25519_sign(audit_private_key, bytes.fromhex(entry_hash)))
```

### Transaction, Checkpoint & Redaction Semantics

- **Shared `Tx` participation:** When `tx` is passed to `audit_log.append(event, tx=tx)`, `SqlAuditLog` flushes into the caller's session and never commits it. If the caller rolls back, neither the audit row nor any post-commit checkpoint is visible.
- **No log, no action:** Any write, trigger, size, or DB error raises `AuditWriteError` (`503 E_AUDIT_WRITE_FAILED`) so the caller aborts and never dispatches a bank operation.
- **Checkpoints:** Written every 50 entries (configurable) and on demand via `await audit_log.checkpoint()` to an independent append-only JSONL file strictly *after* database commit. A local checkpoint detects post-anchor tail truncation on the gateway DB, but is not protection against full host and signing-key compromise.
- **Secret redaction:** Keys such as `pin`, `password`, `secret`, `private_key`, `access_token`, `session_token`, `authorization`, and JWK `d` (as well as PEM private keys, `$argon2` hashes, `Bearer`/`DPoP` headers, and non-`proof_jws` JWT strings) are redacted automatically before hashing and storage.

### Usage Example

```python
from gateway.audit.append import SqlAuditLog
from gateway.audit.checkpoints import CheckpointStore
from gateway.audit.schema import create_audit_schema
from gateway.identity.models import SqlAlchemyTx

create_audit_schema(engine)
audit_log = SqlAuditLog(
    session_factory=session_factory,
    signing_key=audit_private_key,
    clock=clock,
    checkpoint_store=CheckpointStore("runtime/audit_checkpoints.jsonl"),
)

# Join caller-owned transaction (commits/rolls back atomically with business state)
with session_factory() as session:
    with session.begin():
        tx = SqlAlchemyTx(id="tx_1", session=session)
        ref = await audit_log.append(event, tx=tx)

# Anchor head and verify full chain
anchor = await audit_log.checkpoint()
report = await audit_log.verify(anchor=anchor)
assert report.valid
```

---

## B07 & B08 — Evidence Assembly, Audit Routes, Safe Tamper Demo & Integration

### Implemented Modules

- [`gateway/audit/evidence.py`](../../gateway/audit/evidence.py) — `assemble_evidence_bundle` and `build_request_evidence_payload` building `EvidenceBundle` strictly from immutable `audit_log` snapshots without reading Member A/C tables.
- [`gateway/audit/routes.py`](../../gateway/audit/routes.py) — Ops-authenticated FastAPI router (`build_audit_router`) and `503 E_AUDIT_WRITE_FAILED` handler (`install_audit_error_handlers`) for `GET /v1/audit/entries`, `GET /v1/audit/verify`, and `GET /v1/audit/evidence/{request_id}`.
- [`tools/tamper.py`](../../tools/tamper.py) — Safe disposable-copy tamper CLI (`python -m tools.tamper`) enforcing `source != target`, WAL-safe SQLite backup, trigger drop strictly on the disposable copy, and all four S10a–d tamper variants (`edit`, `delete`, `rehash`, `truncate`).
- [`gateway/identity/wiring.py`](../../gateway/identity/wiring.py) — `build_real_member_b_providers`, `assert_distinct_signing_keys`, `assert_real_member_b_providers` (rejecting all Member B fakes in `real`/`scripted` profiles), and `mount_member_b_routes`.
- [`tests/b/test_b07_verify_evidence.py`](../../tests/b/test_b07_verify_evidence.py) — Evidence bundle assembly, ops-only audit routes, offline `proof_jws` verification, and safe tamper tool tests.
- [`tests/integration/test_auth_audit.py`](../../tests/integration/test_auth_audit.py) — Real Member B integration suite covering S6a–h, S10a–d, S11, revocation/reinstatement, write-ahead audit before bank execution, audit failure blocking bank dispatch, real-mode fake rejection, and full secret-leak scanning.

### Verification Output Examples (`GET /v1/audit/verify`)

Valid anchored chain:
```json
{
  "valid": true,
  "entries_checked": 18,
  "head_seq": 18,
  "head_hash": "<64-lowercase-hex>",
  "anchored_through_seq": 18,
  "first_failure": null
}
```

Detected anchored tail truncation (S10d):
```json
{
  "valid": false,
  "entries_checked": 16,
  "head_seq": 16,
  "head_hash": "<64-lowercase-hex>",
  "anchored_through_seq": 0,
  "first_failure": {
    "seq": 18,
    "reason": "tail_truncation: head_seq 16 is below anchored seq 18"
  }
}
```

### Safe Disposable-Copy Tamper CLI (`tools/tamper.py`)

```text
# Never modifies runtime/gateway.db; creates an isolated tampered copy for S10 verification
uv run python -m tools.tamper --source runtime/gateway.db --target runtime/tamper_edit.db --mode edit --seq 2
uv run python -m tools.tamper --source runtime/gateway.db --target runtime/tamper_delete.db --mode delete --seq 3
uv run python -m tools.tamper --source runtime/gateway.db --target runtime/tamper_rehash.db --mode rehash --seq 2
uv run python -m tools.tamper --source runtime/gateway.db --target runtime/tamper_trunc.db --mode truncate --seq 17
```

### S11 Evidence Bundle Example (`GET /v1/audit/evidence/{request_id}`)

```json
{
  "request_id": "req_demo_1",
  "mandate": {"terms": {"mandate_id": "mnd_demo_1", "...": "..."}, "status": "ACTIVE", "mandate_hash": "sha256:...", "sig": "...", "key_id": "mandate-k1"},
  "agent": {"agent_id": "agt_billpal", "name": "BillPal", "vendor": "FinBot Labs", "public_jwk": {"kty": "OKP", "crv": "Ed25519", "x": "..."}, "jkt": "...", "max_scopes": ["bill_pay", "balance_read"], "status": "ACTIVE"},
  "action": {"request_id": "req_demo_1", "customer_id": "cust_001", "agent_id": "agt_billpal", "mandate_id": "mnd_demo_1", "mandate_hash": "sha256:...", "action_type": "bill_pay", "params": {"from_account": "ACC-001", "payee_id": "BILL-ELEC", "amount_paise": 185000, "currency": "INR", "reference": "NOV-2026"}, "intent": {"task": "pay_electricity_bill", "category": "utilities.electricity", "note": "Monthly bill"}, "idempotency_key": "bill-nov-001", "received_at": "2026-11-18T18:00:00Z"},
  "signed_request": {"proof_jws": "<compact-dpop-jws>", "proof_jti": "prf_...", "body_sha256": "<bsh>", "action_hash": "sha256:..."},
  "decisions": [{"seq": 6, "event_type": "policy.decision", "decision": "ALLOW", "reason_codes": [], "risk": {"score": 12, "level": "LOW"}, "safe_payload": {"action_hash": "sha256:..."}, "occurred_at": "2026-11-18T18:00:00Z", "entry_hash": "<64-hex>"}],
  "approval_history": [],
  "bank_outcome": {"status": "EXECUTED", "bank_operation_id": "bank_op_1", "bank_idempotency_key": "bank-bill-nov-001", "reason_code": null, "balance_paise": 12315000, "executed_at": "2026-11-18T18:00:00Z"},
  "audit_references": [{"seq": 5, "entry_hash": "<64-hex>"}, {"seq": 6, "entry_hash": "<64-hex>"}, {"seq": 7, "entry_hash": "<64-hex>"}],
  "verification": {"valid": true, "entries_checked": 18, "head_seq": 18, "head_hash": "<64-hex>", "anchored_through_seq": 18, "first_failure": null}
}
```

### Integration Handoff for Members A, C, and D

- **Member A (`gateway/deps.py`, `gateway/main.py`, `gateway/pipeline.py`, `pyproject.toml`):**
  1. Call `create_identity_schema(engine)` + `create_audit_schema(engine)` on the single shared gateway SQLite WAL database.
  2. Call `providers = build_real_member_b_providers(...)`, enforce `assert_real_member_b_providers(profile=..., ...)` at startup, and call `mount_member_b_routes(app, providers, lifecycle=lifecycle)`.
  3. Pass a `Tx` exposing `.session` (sync `Session` or `AsyncSession`) into `registry.set_status`, `token_service.revoke_for_mandate`, `token_service.revoke_for_agent`, and `audit_log.append`.
  4. Attach `build_request_evidence_payload(mandate=..., agent=..., action=..., signed_request=...)` to `request.received` and `bank_result` to `bank.executed`/`bank.failed`/`bank.unknown` before/after bank calls so `GET /v1/audit/evidence/{request_id}` assembles complete S11 evidence automatically.
  5. Note: [pyproject.toml](../../pyproject.toml) currently pins `sqlalchemy>=2.0,<3` without `[asyncio]` (`greenlet`). Member B works cleanly with sync `Session` under the locked `uv.lock` and will automatically accept `AsyncSession` if Member A adds `sqlalchemy[asyncio]`.
- **Member C (`gateway/escalation/`, `simulator/`):**
  1. Call `HumanAuth.authenticate(token, HumanRole.CUSTOMER)` and `HumanAuth.verify_pin(human.subject_id, pin)` for approval decisions (never handle Argon2 hashes directly).
  2. Record `escalation.created`, `escalation.approved`, `escalation.rejected`, `escalation.expired`, and `escalation.cancelled` through `AuditLog.append(event, tx=tx)` with `request_id` set so S11 `approval_history` populates automatically.
  3. Point `simulator/`'s `ScenarioClient` at `agent_sdk.AgentGateClient` with real Ed25519 keys for S1–S11.
- **Member D (`frontend/`):**
  1. Store human session tokens from `POST /v1/customer/login` and `POST /v1/ops/login` strictly in memory (never `localStorage` or URL query strings) and send `Authorization: Bearer <session_token>`.
  2. Display `VerifyReport.anchored_through_seq` alongside `head_seq` and `first_failure`, and render/download `GET /v1/audit/evidence/{request_id}` JSON directly without reimplementing authority hashing in the browser.

---

### Safe Key Setup Notes

- Never commit private keys, `.env`, tokens, or PINs to Git.
- Separate Ed25519 signing keys must be configured outside source control via `AGENTGATE_MANDATE_SIGNING_KEY_PATH`, `AGENTGATE_AUDIT_SIGNING_KEY_PATH`, and `AGENTGATE_TOKEN_SIGNING_KEY_PATH`, plus distinct per-role keys for `HumanSessionKeyring`.

### Verification Commands

```text
uv run python -m tools.dev check-contracts --owner b
uv run python -m tools.verify_vectors
uv run pytest tests/b
uv run mypy --strict gateway/identity tools/verify_vectors.py tests/b tests/support/b/providers.py
uv run ruff check .
```
