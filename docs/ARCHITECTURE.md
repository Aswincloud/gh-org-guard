# Architecture

A one-page tour of how `gh-org-guard` stays correct without a human.

## Components

| Component | Trigger | Job |
|---|---|---|
| `reconcile_rulesets.py` | weekly cron (enforce) / manual (dry-run) | Reassert **both** rulesets (`org-baseline`, `org-codeowner-review`) + scaffold files on every public repo |
| `auto-approve.yml` | `workflow_call` from each repo's caller | Approve admin PRs with a write-user identity (so the approval counts) |
| `security-sweep.yml` | weekly cron | Audit secret scanning / push protection / open alerts; file a tracking issue |

## The reconcile loop (per repo)

1. **Skip** private/archived/fork/template/empty repos.
2. **Repo settings** — enable `allow_auto_merge` (idempotent).
3. **Find** the existing `org-baseline` and `org-codeowner-review` rulesets (if any).
4. **Ensure files** — CODEOWNERS + auto-approve caller. If a file is missing,
   delete `org-codeowner-review` first (that is the ruleset whose approval gate
   would block a direct write), then continue.
5. **Choose tier** — REVIEW iff both files exist, else FLOOR.
6. **Apply** — `org-baseline` always; `org-codeowner-review` created when REVIEW,
   deleted when FLOOR. POST (new) or PUT (update), and only if the normalized
   current state differs from desired. Classify each repo as
   `in-sync / FLOOR / REVIEW / BLOCKED / error`.

## Why normalization matters

`norm()` compares only the **managed subset** of rule parameters, plus the bypass
list **verbatim**. This lets a repo carry extra, richer rules of its own without
the reconciler seeing "drift" and fighting them every week. It manages a floor; it
doesn't own the whole ruleset.

The bypass list is compared exactly (not just "is an admin present?") because the
whole point of the split is *who* sits on `org-codeowner-review`'s bypass list. A
team silently dropped from it would otherwise read as in-sync.

## Why two rulesets

Rules from all matching rulesets aggregate to the **most restrictive** value, and
bypass is granted **per ruleset, never per rule**. So the only way to let a team
skip code-owner review while still binding them to the merge queue is to put those
two things in *different* rulesets and list the team on just one. Duplicating the
code-owner rule into `org-baseline` would cancel the exemption out silently.

The listing must use `bypass_mode: "exempt"`. `always` and `pull_request` grant an
*override* — GitHub then offers a force-merge that skips the queue, and the green
"Merge when ready" never appears. `exempt` makes the rule not applicable, so the
PR is simply CLEAN and takes the normal queued path.

## Failure modes, by design

- **Missing App permission** → repo reported `BLOCKED` with `needs App
  administration:write`, never silently skipped.
- **No write access to scaffold files** → repo held at FLOOR (mergeable), not
  raised to REVIEW (deadlock-prone).
- **Misconfigured required check** → org admins bypass `org-baseline`, which owns
  the queue. That is the break-glass and it is deliberate.
- **`bypass-team` cannot be resolved** → the run exits non-zero before touching
  anything, rather than writing a code-owner ruleset with an empty bypass list.
