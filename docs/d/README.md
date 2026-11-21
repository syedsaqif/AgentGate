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
