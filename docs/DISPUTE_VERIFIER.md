# Offline dispute verifier

**Export Court-Ready Proof** in Ops Audit downloads a ZIP with a printable QR receipt, proof JSON and the standalone verifier HTML. The existing evidence JSON download is also available. The verifier checks cryptographic integrity and external witness coverage.

**Download QR code** in Ops Audit and Demo Bank opens a receipt dialog with the audit sequence, hash and witness coverage. Save the 512 px PNG, copy the verifier link or print the dialog. The ZIP retains its 360 px receipt QR. Both sizes encode the static verifier URL. Transfer `proof.json` separately from Ops when the recipient needs payment evidence.

## Publish a verifier release

Serve `frontend/verifier/dispute-verifier-v1.html` unchanged as `text/html` from an independent HTTPS static host. Use an immutable versioned URL such as `https://verifier.example.org/agentgate/v1/dispute-verifier-v1.html`. Give the recipient the release URL and the SHA-256 of the HTML through a channel they already trust. `Get-FileHash .\frontend\verifier\dispute-verifier-v1.html -Algorithm SHA256` prints the release hash in PowerShell. The verifier needs a browser with Ed25519 WebCrypto and a secure context.

## Register a log with the witness

Run the witness on a separate machine or OS account under independent control. Keep its PEM and journal private to that operator. Install with `uv sync --frozen` from the repository root on both machines. Export the gateway audit public key and authenticate its identity with the witness before registration.

```powershell
# For a fresh log only, create the persistent gateway audit key before registration.
if (-not (Test-Path -LiteralPath $auditKey)) {
    uv run --frozen python -m agent_sdk.cli keygen --out $auditKey
}
uv run --frozen python -m agent_sdk.cli inspect-key --key $auditKey |
    Set-Content -Encoding utf8 $auditPublic
```

Set `$auditKey` to the stable gateway audit PEM and `$auditPublic` to the intended public JSON file. On the witness machine, set `$witnessDir` to a protected directory outside the checkout and `$auditPublic` to the authenticated copy. Use a unique log ID and one journal per log.

```powershell
$witnessKey = Join-Path $witnessDir 'witness.pem'
$journal = Join-Path $witnessDir 'journal.db'
$trust = Join-Path $witnessDir 'trust.json'
uv run --frozen python -m agent_sdk.cli keygen --out $witnessKey
uv run --frozen python -m tools.audit_witness register --log-id 'payments-demo-01' --audit-public $auditPublic --witness-key $witnessKey --trust $trust
```

Give the recipient `trust.json` separately from the proof. Through an authenticated independent channel, confirm the expected log ID and the printed SHA-256 fingerprint of the trust manifest. This fingerprint pins both authorized public keys. Copy the public manifest to the gateway so it can validate incoming checkpoints.

## Launch and publish coverage

Set `$gatewayDir` to a writable state directory outside the checkout, `$auditKey` to the registered audit PEM, `$trustOnGateway` to the copied manifest and `$checkpointOnGateway` to the path where the signed checkpoint will arrive. For a disposable real-profile demo, run the following from the repository root. Use the installed Python environment and an unused set of state paths.

```powershell
uv sync --frozen
$env:AGENTGATE_PROFILE = 'real'
$env:AGENTGATE_GATEWAY_DB = Join-Path $gatewayDir 'gateway.db'
$env:AGENTGATE_BANK_DB = Join-Path $gatewayDir 'bank.db'
$env:AGENTGATE_BANK_DB_URL = 'sqlite+aiosqlite:///' + $env:AGENTGATE_BANK_DB.Replace('\', '/')
$env:AGENTGATE_AUDIT_SIGNING_KEY_PATH = $auditKey
$env:AGENTGATE_TOKEN_SIGNING_KEY_PATH = Join-Path $gatewayDir 'token.pem'
$env:AGENTGATE_MANDATE_SIGNING_KEY_PATH = Join-Path $gatewayDir 'mandate.pem'
$env:D06_BILLPAL_KEY = Join-Path $gatewayDir 'billpal.pem'
$env:AGENTGATE_DISPUTE_TRUST_PATH = $trustOnGateway
$env:AGENTGATE_DISPUTE_CHECKPOINT_PATH = $checkpointOnGateway
$env:AGENTGATE_BANK_SERVICE_CREDENTIAL = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
$env:D06_CUSTOMER_PIN = [Net.NetworkCredential]::new('', (Read-Host 'Disposable Customer PIN' -AsSecureString)).Password
$env:D06_OPS_PASSWORD = [Net.NetworkCredential]::new('', (Read-Host 'Disposable Ops password' -AsSecureString)).Password
uv run --frozen python -m tools.seed --profile real
uv run --frozen python -m tools.dev stack --profile real
```

In another terminal, set `$verifierReleaseUrl` to the trusted, independently hosted HTTPS URL. Run from `frontend/`.

```powershell
npm ci
$env:VITE_GATEWAY_ORIGIN = 'http://127.0.0.1:8000'
$env:VITE_DISPUTE_VERIFIER_URL = $verifierReleaseUrl
$env:AGENTGATE_DISPUTE_VERIFIER_URL = $verifierReleaseUrl
npm run dev:real -- --strictPort
```

Use the registered BillPal key from `$gatewayDir` for the signed payment and approve it in Customer. Keep the Customer receipt. The [Windows payment walkthrough](d/README.md) has the mandate values and SDK command.

The witness reads a protected read-only SQLite replica or consistent snapshot with the audit table. Provision this separately from the public proof. Use SQLite's online backup facility for a live source. Keep the replica on the restricted operator channel. Set `$auditReplica` to that file and `$publication` to a new filename, then run the witness manually or from the operator's scheduler.

```powershell
uv run --frozen python -m tools.audit_witness checkpoint --audit-db $auditReplica --trust $trust --witness-key $witnessKey --journal $journal --output $publication
```

The witness recomputes the source record hashes and checks audit signatures. SQLite serializes publishers and protects the append-only journal. The witness accepts contiguous extensions, returns the same checkpoint for the same head, and rejects rollback, forks and changed registrations. Protect the journal against file replacement and retain independent copies of the latest publication. If restoring storage, reconcile with the last retained signed checkpoint before publishing again.

The public checkpoint signs the log ID, sequence, head hash, ordered link commitment and UTC publication time. A Gist can carry a copy of a signed publication. The witness journal and independently retained publications establish its history. Deliver the signed file to `$checkpointOnGateway` with an atomic replacement so the gateway can read it on export.

## Verify on another device with AgentGate offline

1. In Ops Audit, select the disputed payment's evidence and click **Export Court-Ready Proof**. Open `receipt.html` from the ZIP and print it from the browser. It shows the Customer request ID, sequence and hash, and witness coverage. Its QR carries only the static release URL.
2. Transfer `proof.json` to the recipient's Downloads or Files app through a suitable file channel. They receive the public trust manifest and fingerprint through the independent channel. They can also receive the verifier HTML separately for a fully offline start.
3. Scan the receipt QR and confirm the HTTPS release through the trusted channel. Load the page before going offline. For a fully offline start, open an independently authenticated copy of the HTML in a browser that supports WebCrypto for local files. Disable networking. AgentGate and the witness can be stopped.
4. Import `proof.json` and `trust.json`, enter the independently confirmed fingerprint and optionally import an independently obtained checkpoint. Click **Verify proof** and compare the displayed request, sequence and hash with the receipt.

**Verified against external witness** covers the disclosed payment as of the signed checkpoint. **Valid chain without external anchoring** shows that the authorized audit key signed the supplied chain. **Verification failed** identifies the rejected check. A configured missing, invalid or too-old checkpoint prevents export. Set only `AGENTGATE_DISPUTE_TRUST_PATH` to intentionally enable an unanchored export.

The browser recomputes the released audit record hash and verifies all signed chain links, the audit seal and the witness signature. The released record commits to the original Customer receipt hash. Opaque links keep other records private. The witness verifies their full canonical hashes from its protected source. A genuine checkpoint can be presented later. For coverage through a newer checkpoint, obtain the checkpoint independently and export a proof that reaches it. Importing a newer checkpoint beyond the supplied prefix fails. These checks establish ledger integrity and coverage. Decisions about the facts and admissibility belong to the recipient.

Version 1 supports newly recorded successful money-out operations, including recovered executions, with one audit signing key and up to 10,000 links from genesis. Historical payments without a signed disclosure still support the existing evidence JSON. The additive Ops endpoint is `GET /v1/audit/proof/{request_id}`. The frozen API schema and original evidence format retain their existing versions.
