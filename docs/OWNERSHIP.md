# Repository ownership

This document records role ownership until GitHub usernames are supplied. It does not invent usernames, and therefore cannot yet enforce review routing through CODEOWNERS.

## Member A — enforcement and integration

Owns `contracts/common.py`, `contracts/core_*`, contract aggregates, `contracts/http/a.yaml`, fixture A, gateway composition files, `gateway/platform`, `gateway/mandates`, `gateway/executor`, `gateway/events`, `bank_mock`, `tests/a`, A integration tests/support, `tools/dev.py`, `tools/seed.py`, `tools/reset.py`, `tools/export_openapi.py`, `docs/a`, root Python/config files, `.github/workflows/a.yml`, and `.github/workflows/integration.yml`.

## Member B — identity and audit

Owns `contracts/identity_*`, `contracts/audit_*`, `contracts/canonical.py`, `contracts/reason_codes.py`, `contracts/http/b.yaml`, fixture B, `gateway/identity`, `gateway/audit`, `agent_sdk`, `tests/b`, `tests/integration/test_auth_audit.py`, `tests/support/b`, `tools/tamper.py`, `tools/verify_vectors.py`, `docs/b`, and `.github/workflows/b.yml`.

## Member C — risk and approval

Owns `contracts/risk_*`, `contracts/escalation_*`, `contracts/http/c.yaml`, fixture C, `gateway/risk`, `gateway/escalation`, `simulator`, `tests/c`, C integration tests/support, `tools/metrics.py`, `docs/c`, and `.github/workflows/c.yml`.

## Member D — frontend and demo

Owns fixture D, all of `frontend`, `docs/d`, `docs/DEMO.md`, `docs/TEST_RUN.md`, the user-facing portions of `README.md`, and `.github/workflows/d.yml`.

## Shared/frozen

All four review `contracts/`, `.github/CODEOWNERS`, and contract-semantic changes. Member A is merge coordinator for shared Python config, contract aggregates, and the composition root. All four approve contract/shared semantic changes; normal private module refactors need only the normal cross-track review.

See the source task distribution for exact file-level exceptions and acceptance ownership.
