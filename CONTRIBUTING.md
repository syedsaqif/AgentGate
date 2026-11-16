# Contributing to AgentGate

Read the task distribution and `docs/OWNERSHIP.md` before changing code. `main` is the integration branch and should remain releasable; routine development happens through short-lived branches and pull requests.

## Branch names

Use the track and task identifier from the plan:

- `track-a/A04-policy`
- `track-b/B03-proof`
- `track-c/C05-approval`
- `track-d/D03-inbox`

Use lowercase after the task ID. Do not force-push `main`. If a published personal branch must be rebased, coordinate first and use `--force-with-lease`.

## File ownership

- Edit your exclusive paths directly on your track branch.
- Do not edit another member's implementation to unblock yourself; add a failing fixture/test in your owned area and notify that owner.
- Shared contracts, root configuration, lockfiles, the composition root, and CODEOWNERS are serialized changes coordinated by Member A.
- `contracts/common.py`, contract aggregates, and HTTP contracts freeze after IC0. Any semantic change needs all four members' agreement and an entry in `docs/CONTRACT_CHANGES.md`.
- Member D alone changes `frontend/package.json` and `frontend/package-lock.json`; Member A coordinates `pyproject.toml` and `uv.lock`.

## Pull requests

Every PR must state the behavior, owned paths, contract version, provider mode, tests run, environment/schema changes, and one runnable example. Keep it focused and update it against current `main` before merge.

- Normal owner PR: at least one reviewer from another track; the owning member agrees if someone else authored the change.
- Contract/shared semantics PR: author signs off in the PR description and the other three members approve the latest revision. Update providers, consumers, fixtures, and `docs/CONTRACT_CHANGES.md` together or add a backward-compatible adapter.
- Never merge a red required check. A skeleton/mock check is not a substitute for a real-provider security or integration check.

Use squash merge for focused feature PRs unless preserving commits is useful for an integration investigation. Delete merged feature branches. Revert a bad isolated merge instead of leaving `main` broken.

## Local checks

Run the shared bootstrap checks plus your owner suite. Member D also runs frontend typecheck, tests, and production build. Once IC2 is reached, all-real integration and scenario gates may not be replaced with fakes.

## Security and data handling

Do not commit private keys, PINs beyond explicitly disposable public demo seed instructions, access/session tokens, credentials, local databases, audit checkpoints, recordings, or evidence output. Test private keys, if later required for vectors, must be unmistakably test-only and rejected by the demo profile.
