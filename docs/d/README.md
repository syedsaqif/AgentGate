# Member D handoff notes

## D06 real browser smoke

Run in PowerShell from the repository root with Python 3.12, uv, Node 24 and npm 11.

```powershell
uv sync --frozen
Set-Location frontend
npm ci
npx playwright install chromium
npm run smoke:real
```

The command invokes the project seed, the loopback simulated bank, the gateway through `tools.dev service a --profile real`, and Vite in real mode. Each run creates a fresh temporary working directory for the relative SQLite URLs in `config.yaml`, uses separate loopback ports, and removes its data on completion. The simulated bank runs its own startup seed. The runner checks gateway readiness, contract version and provider modes. Chromium opens the normal Customer and Ops routes in separate contexts. The process exits with status 1 for any failed or blocked requirement. The fast suite runs separately with `npm test`.

For an integrated seed, set `D06_OPS_ID` and `D06_OPS_PASSWORD` to the disposable operator credentials and `D06_BILLPAL_KEY` to the absolute path of the seed's registered BillPal PEM. These are runner inputs. The Customer login uses the public Customer persona from §13.2 and accepts a `D06_CUSTOMER_PIN` override. Leave all four unset to reproduce the startup blockers on backend `3a18e74`. When that gateway refuses startup, the Ops page uses a disposable diagnostic login to exercise its connection error. The run produces console results only.

### Acceptance and provider handoff

The Customer creates a bill-payment mandate for BillPal, ACC-001 and BILL-ELEC with a ₹5,000 transaction cap, ₹10,000 lifetime cap and ₹4,000 approval threshold. The runner is wired to pass the mandate ID from A's 201 response to B's `pay-bill` CLI for one ₹4,800 action and leave approval to the browser.

On B's unmerged provider branch `f4c0542`, the Python API also supports this flow. Construct `AgentGateClient` with the browser-created `mandate_id` and call `pay_bill` to send a signed `POST /v1/payments/bills`. Approval remains with the browser. On the integrated revision, `agent_sdk/client.py`, `keys.py`, `proofs.py` and `cli.py` are placeholders. C's scenario client is a fixture, and `run_s09a` resets state, uses a fixed mandate and approves the payment itself.

The browser assertions cover one approval POST, a correlated receipt, refetched revocation, one use committing 480000 paise with zero reservation, and matching Ops evidence. These assertions are pending a real transaction.

- **A's bank test** `test_bank_payment_same_key_debits_once` passed. It sent two identical payments with the same idempotency key to the simulated bank via ASGI. It verified the same operation ID, one ledger entry for 185000 paise, and a final balance of 12315000 paise from a 12500000-paise seed.
- **C's S9 test** `test_scenario_s09_approval_lifecycle` passed. S9a checks that the fixture balance moves from 12500000 to 12020000 paise. It does not count persisted ledger rows.
- **D06 transaction** still needs a provider-side check that links the operation in Ops evidence to exactly one persisted debit and a balance reduction of 480000 paise. Both tests above used separate transactions. Ledger access stays outside Chromium.

### Owner handoff

The combined smoke with backend `daca3fdb00c15cf1e761bc1a51364a6754d8ab2d` exited 1. The disposable seed and simulated bank startup passed, and both Chromium workspaces rendered. The real gateway refused to start without `identity_verifier`, `human_auth`, and `audit_log`. Both sign-in attempts got no HTTP response and showed `UI_GATEWAY_UNREACHABLE`. The checkout also lacks B's signed `pay-bill` CLI. Authenticated steps and live receipt correlation remain pending.

| Stage and owner | Observed blocker and capability needed |
| --- | --- |
| Seed (A with B) | The disposable simulated bank seed passes. Verify that the integrated seed supplies both browser accounts and the registered BillPal signing material once B's providers are available. |
| Startup and readiness (A with B) | The real guard reports missing identity verification, human authentication, and audit. Wire the real providers so `tools.dev service a --profile real` serves `/health/ready` with their real modes. |
| Signed action (B) | Integrate the signed CLI that accepts the browser-created mandate ID. Verify all required application routes after the gateway reaches readiness. |
| Receipt after 202 (A/C) | A Customer-authorized stream handler must emit a matching §6.8 `bank.executed` receipt. The frozen Customer list reads pending items only, so a receipt lost beyond replay has no published durable lookup. The IC0 snapshot still lacks human success-body and SSE payload schemas. |

Retry the real smoke with fresh disposable data when a team-approved revision provides the seed and signing inputs, passes real readiness, and mounts the routes for the flow.
