# AgentGate

AgentGate is a four-track hackathon prototype for controlling bank actions requested by AI agents. The Customer and Ops frontend can be walked through with fixtures. Real-stack demo acceptance is pending provider integration.

Private GitHub repository: <https://github.com/syedsaqif/AgentGate>

The source of truth is [TEAM_TASK_DISTRIBUTION (28).md](./TEAM_TASK_DISTRIBUTION%20%2828%29.md).

## Architecture and ownership

| Track | Owner | Exclusive implementation areas |
|---|---|---|
| A · Enforcement and integration | Member A | `gateway/platform`, `gateway/mandates`, `gateway/executor`, `gateway/events`, `bank_mock`, root Python/config tooling, A tests |
| B · Identity and audit | Member B | `gateway/identity`, `gateway/audit`, `agent_sdk`, B tests and verification tools |
| C · Risk and approval | Member C | `gateway/risk`, `gateway/escalation`, `simulator`, C tests and metrics |
| D · Frontend and demo | Member D | `frontend`, D fixtures/docs, demo and UI verification |

Shared contracts are frozen at version `1.0.0`. See [ownership](docs/OWNERSHIP.md), [architecture](docs/ARCHITECTURE.md), and [contributing](CONTRIBUTING.md).

## Windows PowerShell quick start

### Mock UI

Use Git, Node.js 24 and npm 11. From the root of `track-d/D07-demo` (base `468237b`), run:

```powershell
Set-Location frontend
npm ci
npm run dev:mock
```

Open `http://127.0.0.1:5173/customer` or `http://127.0.0.1:5173/ops` using the port printed by Vite. Both display **Mock mode · Fixture data**. The disposable identities are prefilled. Use PIN `4821` for Customer and password `agentgate-demo` for Ops. Mock mode requires no backend, Python, or environment variables.

### Real-stack smoke

Use Python 3.12 and uv 0.13.x in addition to Node.js 24 and npm 11. Run from the repository root. The frontend package includes the D06 smoke command and its Playwright dependency.

```powershell
uv sync --frozen
Set-Location frontend
npm ci
npx playwright install chromium
npm run smoke:real
```

The smoke starts a disposable loopback stack with the real gateway startup guard enabled. For the integrated seed, supply `D06_OPS_ID`, `D06_OPS_PASSWORD` and `D06_BILLPAL_KEY` (absolute path to the seeded BillPal PEM) in the runner environment. `D06_CUSTOMER_PIN` optionally overrides the disposable seed PIN. The runner sets `VITE_GATEWAY_ORIGIN` to its gateway. For an interactive real UI with a gateway already running, set `$env:VITE_GATEWAY_ORIGIN = 'http://127.0.0.1:8000'` to the actual origin and run `npm run dev:real` from `frontend/`.

With backend `daca3fd`, the disposable seed and simulated bank start. Gateway readiness still requires B's real identity, human authentication, and audit providers. The signed payment CLI also remains pending. See the [D06 runbook](docs/d/README.md) and [verification log](docs/TEST_RUN.md).

## Bootstrap checks

From the repository root:

```powershell
uv sync --frozen
uv run python -m tools.dev check-contracts
uv run pytest -m "scaffold or contract"

Set-Location frontend
npm ci
npm run typecheck
npm test -- --run
npm run build
```

The bootstrap checks cover the scaffold and frozen contract. Real acceptance evidence is tracked separately.

## Development workflow

1. Start from current `main` with `git switch main` and `git pull --ff-only`.
2. Create an owned branch such as `track-a/A01-foundation`, `track-b/B01-vectors`, `track-c/C01-contracts`, or `track-d/D01-shell`.
3. Change only owned paths unless a coordinated contract PR explicitly covers shared files.
4. Run the owner checks plus shared contract checks.
5. Push the branch and open a pull request using the repository template. Use GitHub CLI as needed for repo and ruleset administration.

Never commit `.env`, private keys, tokens, credentials, SQLite/WAL files, checkpoints, recordings, or runtime evidence. `.env.example` contains names and unsafe placeholders only.

## Documentation

- [Contributing](CONTRIBUTING.md) for branches, reviews, PRs, and contract changes
- [Ownership](docs/OWNERSHIP.md) for path and database ownership
- [Architecture](docs/ARCHITECTURE.md) for integration boundaries and provider seams
- [Contract changes](docs/CONTRACT_CHANGES.md) for contract version history
- [GitHub setup](docs/GITHUB_SETUP.md) for private repo and branch protection
- [Demo guide](docs/DEMO.md) for the seven-minute script and results slide
- [Verification log](docs/TEST_RUN.md) for verification evidence
