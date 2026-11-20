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

The latest fetch still resolves `origin/main` to the previously tested `3a18e749056c4af1a46c6efa2c0385db6394970f`. The existing smoke run exited 1. The simulated bank started and both Chromium workspaces rendered. The gateway never returned a readiness payload. Both browser sign-in attempts got no HTTP response and showed `UI_GATEWAY_UNREACHABLE`. Authenticated endpoints were blocked by startup, so their HTTP behavior remains unmeasured. The unchanged provider revision did not warrant an identical rerun.

| Stage and owner | Observed blocker and capability needed |
| --- | --- |
| Startup and seed (A with B) | The supported launcher is `tools.dev service a --profile real`. `tools.seed` exited 1 as a placeholder. Supply a disposable gateway seed with both browser accounts and a registered BillPal signer. |
| Startup and readiness (A with B/C) | The real guard refused startup because identity verification, human auth, audit, policy, risk, escalation and the bank client were missing. Wire the real providers so the supported launcher serves `/health/ready` with their real modes. |
| Mounted routes and signer (A/B/C) | The composition root mounts only health. A must mount the mandate, payment and events routes, B's login and audit routes, and C's escalation router with real ports. Integrate B's SDK path that accepts the browser mandate ID. |
| Receipt after 202 (A/C) | A Customer-authorized stream handler must emit a matching §6.8 `bank.executed` receipt. The frozen Customer list reads pending items only, so a receipt lost beyond replay has no published durable lookup. The IC0 snapshot still lacks human success-body and SSE payload schemas. |

Retry the real smoke with fresh disposable data when a team-approved revision provides the seed and signing inputs, passes real readiness, and mounts the routes for the flow.
