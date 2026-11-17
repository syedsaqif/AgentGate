# GitHub setup

GitHub usernames were unavailable during bootstrap, so `.github/CODEOWNERS` intentionally contains instructions but no fake handles. Before enabling required code-owner review, replace the documented role map with all four real `@handles`.

## Current repository status

- Private repository: <https://github.com/syedsaqif/AgentGate>
- Default branch: `main`
- Bootstrap CI is active.
- On 17 November 2026, GitHub rejected creation of a private-repository ruleset with HTTP 403 and required GitHub Pro or a public repository. The repository remains private. Until the account supports rulesets, enforce the PR-only workflow manually; do not push routine development directly to `main`.

## Repository creation

From the repository root after installing and authenticating GitHub CLI:

```text
gh auth login --web --git-protocol https
gh repo create AgentGate --private --source . --remote origin --push
```

If that exact name is already taken, choose a name with the team before creating the remote. Do not invite collaborators until the owner requests it.

## Main protection

In GitHub: **Settings → Rules → Rulesets → New branch ruleset**. Target the default branch `main`, then enable pull requests, block force pushes and deletions, require conversation resolution, require the relevant owner and integration checks, and require one approval for normal module PRs. Contract/shared PRs still require explicit approval from the other three members because CODEOWNERS cannot express four-person unanimity by itself.

If account/repository rulesets are unavailable, use the same PR-only discipline manually and never push directly to `main` after bootstrap.
