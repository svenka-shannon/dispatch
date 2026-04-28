---
name: upstream
description: Use when the user asks to "upstream" a feature, fix, or change. Identifies the relevant commits on this fork's main that aren't yet in the upstream repo, isolates them onto a clean branch from upstream/main, and opens (and publishes) a PR against the upstream repo. Works in any repo that has both `origin` (your fork) and `upstream` (canonical) remotes plus an authenticated `gh` CLI.
allowed-tools: Bash(~/.claude/skills/upstream/scripts/upstream:*), Bash(git:*), Bash(gh:*)
---

# Upstream Skill

Send a feature or fix from this fork's `main` back upstream as a clean PR — without including any of the fork's local-only changes.

## When to use

Trigger phrases: "please upstream X", "send X upstream", "open an upstream PR for X", "PR X to upstream".

Don't use for: pushing changes to your own fork (those go straight to `origin/main`), or for upstream PRs in a repo that isn't a fork.

## Steps you (Claude) take

### 1. Find the candidate commit(s)

Run:
```bash
git fetch upstream
git log --oneline upstream/main..origin/main
```

Match the user's description ("feature X", "the env loader fix", etc.) against commit subjects and file diffs. **Skip any commit whose subject starts with `[fork]`** — those are fork-only by convention.

If multiple commits could match, ask the user to confirm before proceeding.

### 2. Confirm with the user

Show the SHA, subject, and diff stat (`git show --stat <sha>`) for the commit(s) you identified. Wait for confirmation before publishing.

### 3. Run the helper script

For one commit:
```bash
~/.claude/skills/upstream/scripts/upstream <sha>
```

For multiple commits (cherry-picked in order onto one branch):
```bash
~/.claude/skills/upstream/scripts/upstream <sha1> <sha2> ...
```

Useful flags:
- `--name <branch>` — override the auto-generated branch name
- `--draft` — open as a draft PR
- `--web` — open the PR composer in a browser instead of publishing
- `--title <title>` / `--body <body>` — override the PR title/body (default: derived from the commit subject and message)

The script: fetches upstream, refuses if the commit is already upstream, creates `upstream/<slug>` from `upstream/main`, cherry-picks the commit(s), pushes to `origin`, runs `gh pr create` to **publish** the PR (default — not a preview), and switches back to your previous branch.

### 4. Report the PR URL

The script prints the published PR URL on success. Pass it back to the user.

## On cherry-pick conflict

If the cherry-pick fails, the commit likely depends on other fork-only changes that aren't upstream. The script aborts and leaves the conflict branch checked out. Help the user by:

1. Inspecting the conflict (`git status`, the offending file).
2. Deciding whether to:
   - **Split the commit** — if it bundled upstream-relevant code with fork-only edits, the original commit on fork main should be amended to be cleaner (history rewrite — confirm with user first).
   - **Bring along a dependency commit** — re-run with both SHAs.
   - **Adapt manually** — resolve, `git cherry-pick --continue`, then `git push origin <branch>` and `gh pr create ...`.

## Conventions baked into the workflow

- `[fork]` subject prefix → fork-only, never upstream-eligible.
- Atomic commits (one logical change each) → cherry-pick cleanly.
- The branch `upstream/<slug>` is *only* a vehicle for the PR. Don't merge it back into fork main; the commit is already there. Delete it after the PR closes.
