# Architecture

A one-page tour of how `gh-org-guard` stays correct without a human.

## Components

| Component | Trigger | Job |
|---|---|---|
| `reconcile_rulesets.py` | weekly cron (enforce) / manual (dry-run) | Reassert the baseline ruleset + scaffold files on every public repo |
| `auto-approve.yml` | `workflow_call` from each repo's caller | Approve admin PRs with a write-user identity (so the approval counts) |
| `security-sweep.yml` | weekly cron | Audit secret scanning / push protection / open alerts; file a tracking issue |

## The reconcile loop (per repo)

1. **Skip** private/archived/fork/template/empty repos.
2. **Repo settings** — enable `allow_auto_merge` (idempotent).
3. **Find** the existing `org-baseline` ruleset (if any).
4. **Ensure files** — CODEOWNERS + auto-approve caller. If the repo is already at
   REVIEW but a file is missing, drop it to FLOOR first so the write is allowed,
   then continue.
5. **Choose baseline** — REVIEW iff both files exist, else FLOOR.
6. **Apply** via POST (new) or PUT (update) — but only if the normalized current
   state differs from desired. Classify each repo as
   `in-sync / FLOOR / REVIEW / BLOCKED / error`.

## Why normalization matters

`norm()` compares only the **managed subset** of rule parameters (plus whether the
admin bypass is present). This lets a repo carry extra, richer rules of its own
without the reconciler seeing "drift" and fighting them every week. It manages a
floor; it doesn't own the whole ruleset.

## Failure modes, by design

- **Missing App permission** → repo reported `BLOCKED` with `needs App
  administration:write`, never silently skipped.
- **No write access to scaffold files** → repo held at FLOOR (mergeable), not
  raised to REVIEW (deadlock-prone).
- **Misconfigured required check** → org-admin always-bypass is the break-glass.
