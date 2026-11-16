# GitHub setup

GitHub usernames were unavailable during bootstrap, so `.github/CODEOWNERS` intentionally contains instructions but no fake handles. Before enabling required code-owner review, replace the documented role map with all four real `@handles`.

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
