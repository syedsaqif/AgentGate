# AgentGate

AgentGate is a four-track hackathon prototype for controlling bank actions requested by AI agents. This repository is at the **IC0 bootstrap stage**: architecture, ownership, contracts, fixtures, developer tooling, and CI scaffolding exist; product and security features do not.

The source of truth is [`TEAM_TASK_DISTRIBUTION (28).md`](./TEAM_TASK_DISTRIBUTION%20(28).md). Do not treat passing scaffold checks as evidence that any security capability is implemented.

## Architecture and ownership

| Track | Owner | Exclusive implementation areas |
|---|---|---|
| A — enforcement/integration | Member A | `gateway/platform`, `gateway/mandates`, `gateway/executor`, `gateway/events`, `bank_mock`, root Python/config tooling, A tests |
| B — identity/audit | Member B | `gateway/identity`, `gateway/audit`, `agent_sdk`, B tests and verification tools |
| C — risk/approval | Member C | `gateway/risk`, `gateway/escalation`, `simulator`, C tests and metrics |
| D — frontend/demo | Member D | `frontend`, D fixtures/docs, demo and UI verification |

Shared contracts are frozen at version `1.0.0`. See [`docs/OWNERSHIP.md`](docs/OWNERSHIP.md), [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), and [`CONTRIBUTING.md`](CONTRIBUTING.md) before editing.

## Prerequisites

- Python 3.12 (the application baseline)
- uv 0.13.x
- Node.js 24 and npm 11 for the frontend
- Git
- GitHub CLI for repository creation and branch/ruleset administration

## Bootstrap checks

```text
uv sync --frozen
uv run python -m tools.dev check-contracts
uv run pytest -m "scaffold or contract"

cd frontend
npm ci
npm run typecheck
npm test -- --run
npm run build
```

The service, reset, scenario, and real-stack commands described in the task distribution are reserved interfaces. Their feature implementations belong to the respective tracks and are intentionally not present in this initialization commit.

## Development workflow

1. Start from current `main`: `git switch main && git pull --ff-only`.
2. Create an owned branch: `track-a/A01-foundation`, `track-b/B01-vectors`, `track-c/C01-contracts`, or `track-d/D01-shell`.
3. Change only owned paths unless a coordinated contract PR explicitly covers shared files.
4. Run the owner checks plus shared contract checks.
5. Push the branch and open a pull request using the repository template.

Never commit `.env`, private keys, tokens, credentials, SQLite/WAL files, checkpoints, recordings, or runtime evidence. `.env.example` contains names and unsafe placeholders only.

## Documentation

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — branches, reviews, PRs, and contract changes
- [`docs/OWNERSHIP.md`](docs/OWNERSHIP.md) — complete path and database ownership
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — integration boundaries and provider seams
- [`docs/CONTRACT_CHANGES.md`](docs/CONTRACT_CHANGES.md) — contract version history
- [`docs/GITHUB_SETUP.md`](docs/GITHUB_SETUP.md) — private repo and branch-protection setup
- [`docs/DEMO.md`](docs/DEMO.md) — Member D placeholder; no feature claims
- [`docs/TEST_RUN.md`](docs/TEST_RUN.md) — verification evidence log
