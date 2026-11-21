# Member D real-stack runbook

Use Windows PowerShell, Python 3.12, uv 0.13.x (at least 0.13.7), Node 24 and npm 11. Run each terminal from the repository root.

## Interactive demo

In the first terminal, install the locked dependencies and start the services.

```powershell
uv sync --frozen
npm --prefix frontend ci
powershell -NoProfile -File .\tools\demo.ps1
```

Choose a disposable Customer PIN and Ops password at the masked prompts. The launcher passes the credentials to the real seed, generates a bank service credential, and creates fresh SQLite databases and Ed25519 keys in a new temporary directory. It prints the SDK key file path. The gateway runs on port 8000 and the simulated bank on port 8001. Keep this terminal open.

In the second terminal, start the real Customer and Ops workspaces.

```powershell
Set-Location frontend
$env:VITE_GATEWAY_ORIGIN = 'http://127.0.0.1:8000'
npm run dev:real -- --strictPort
```

The PowerShell environment variable supplies the gateway origin for this Vite session.

Open `http://127.0.0.1:5173/customer` and `http://127.0.0.1:5173/ops` in separate browser contexts. Sign in as `cust_001` and `ops_admin` using the values chosen at startup. Create a BillPal bill-payment mandate for ACC-001 and BILL-ELEC with a ₹5,000 transaction cap, ₹10,000 lifetime cap, five uses, future expiry and ₹4,000 approval threshold. Confirm with the Customer PIN.

In a third terminal, enter the key file path from the first terminal and the mandate ID from the Customer workspace.

```powershell
$keyFile = Read-Host 'BillPal PEM file path'
$mandateId = Read-Host 'Customer mandate ID'
uv run python -m agent_sdk.cli pay-bill --gateway-url http://127.0.0.1:8000 --agent-id agt_billpal --key $keyFile --mandate-id $mandateId --from-account ACC-001 --payee-id BILL-ELEC --amount-paise 480000 --task 'Pay electricity bill' --category utilities.electricity
```

Refresh the Customer inbox. Approve the matching pending request once, then compare the receipt sequence and hash with Ops evidence for that request. Verify the audit chain, download the evidence JSON and revoke the mandate. Stop both services with Ctrl+C. Starting the launcher again creates fresh state.

## Payment gate demo

Open `http://127.0.0.1:5173/demo/gate`. The default stream is the local demo relay at `http://127.0.0.1:8002/demo/gate/stream`. Set `VITE_DEMO_GATE_STREAM_URL` before starting Vite if the relay uses another address. Start the relay and a gateway with the Phase 1 `gate.step` producer, then open the page before submitting the payment. Use the request selector to revisit interleaved requests or open the route with `?request_id=<id>` to follow one request.

The **Trace source** menu also works with the services stopped. Routine payment, customer approval, and policy denial with suspension were captured from a disposable real-profile stack with a seeded bank and signed SDK requests. The cap denial comes from the Phase 1 redacted sample. Identity rejection is labeled as a fixture and reproduces the checks in Phase 1's `test_identity_trace_stops_at_aggregate`.

## Demo Bank

With the demo stack and Vite running, open `http://127.0.0.1:5173/demo/bank`. Sign in as `cust_001` with the PIN chosen when you started the stack. For a different demo server port, set `VITE_DEMO_BANK_ORIGIN` before starting Vite. Use the same loopback hostname for both services so the browser can use the demo session cookie.

Go to **Mandates** → **Write a mandate** → **Use example** → **Parse terms**. Review or edit the fields, then sign with the Customer PIN. Approvals and recent bank transactions are on **Home**.

## Payment assistant

Open `http://127.0.0.1:5173/demo/agent` with the demo backend running. For another backend address, set `VITE_DEMO_AGENT_ORIGIN` before starting Vite. The Test lab loads the backend catalogue. **Use prompt** opens an editable draft. **Run** starts the selected scenario. Identity and integrity fixtures open as read-only results.

Run a successful bill payment before P6. The retry uses that bill's original payload and idempotency key. Reopening a chat reads the existing run. A soft reset signs out the demo bank and clears the retry cache while retaining balances, ledger entries and risk history.

## Automated acceptance

```powershell
uv sync --frozen
Set-Location frontend
npm ci
npx playwright install chromium
npm run smoke:real
```

The runner supplies its own secrets, seeds both databases, starts the same real stack on disposable loopback ports and opens two Chromium contexts. It checks the signed action, live Ops decision, Customer POST and streamed receipt, refreshed usage and revocation, downloaded evidence, and persisted bank operation and ledger row. It also exercises Ops suspension and reinstatement. Only public correlation IDs, receipt hashes, amounts and statuses belong in the run log.

Fixture verification uses `npm test -- --run`. The hosted integration workflow runs both suites. See [milestone evidence](../TEST_RUN.md) and the [seven-minute script](../DEMO.md).
