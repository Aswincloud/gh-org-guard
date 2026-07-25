# gh-org-guard

**Self-healing governance for a GitHub organization.** One scheduled workflow keeps
*every* public repo in an org on the same baseline — branch protection, merge queue,
required reviews, an auto-approver so a solo owner is never deadlocked, and a weekly
secret-hygiene sweep.

![CI](https://img.shields.io/badge/actionlint-clean-2ea44f)
![License](https://img.shields.io/badge/license-MIT-blue)
![Python](https://img.shields.io/badge/python-3.10%2B-3776ab)
![Platform](https://img.shields.io/badge/platform-GitHub%20Actions-24292e)

> This is a **reference implementation** — the exact system running across a real
> 20-plus-repo org. Read it, copy the pieces you need, adapt the constants. It is
> intentionally small (one ~300-line script + a handful of workflows) so you can
> understand the *whole* thing, not a framework.

---

## Why this exists

Managing one repo's settings by hand is fine. Managing twenty is not — settings
drift, new repos start unprotected, and a leaked secret can sit in a public repo
for a year before anyone notices.

Two real incidents motivated this:

- A **live API token** sat in a public repo's git history for **12 months**. Secret
  scanning *caught it the same minute it was committed* — but the alert was later
  dismissed as "revoked" while the token stayed live. Detection worked; the
  remediation discipline didn't.
- A **second live token** was found in a different public repo — invisible only
  because secret scanning happened to be **off** on that one repo.

The lesson isn't "scan harder." It's that **security posture has to be reconciled,
not configured once**. `gh-org-guard` is that reconciler.

---

## What it enforces

Across every **public, active** repo in the org, on a weekly schedule:

| Area | Guarantee |
|---|---|
| **Force-push / deletion** | Blocked on the default branch |
| **Pull requests** | Required, with conversation-resolution |
| **Reviews** | 1 approval + code-owner review — *once the repo can be auto-approved* |
| **Merge queue** | Squash, all-green grouping, solo-owner friendly (no batching wait) |
| **Break-glass** | Org admins can always bypass — a bad required check can't permanently lock a repo |
| **Auto-merge capability** | `allow_auto_merge` enabled (you still click "Merge when ready") |
| **Secret scanning** | Weekly audit: scanning on, push protection on, zero open alerts |

It **stacks** with any richer per-repo ruleset — GitHub applies all matching
rulesets, so this only ever *adds* a floor; it never removes a repo's own rules.

---

## Two ways to run it

**As a GitHub Action** (fastest) — drop this into a workflow in your org's `.github`
repo. It defaults to **dry-run**, so a first run only prints what it *would* do:

```yaml
name: Reconcile org baseline
on:
  schedule:
    - cron: "17 6 * * 1"   # Mondays 06:17 UTC
  workflow_dispatch:
    inputs:
      dry-run: { type: boolean, default: true }

jobs:
  reconcile:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/create-github-app-token@v3
        id: app
        with:
          app-id: ${{ secrets.APP_ID }}
          private-key: ${{ secrets.APP_PRIVATE_KEY }}
          owner: ${{ github.repository_owner }}

      - uses: Aswincloud/gh-org-guard@v1
        with:
          org: ${{ github.repository_owner }}
          # schedule => enforce; manual => whatever you pick (default: true)
          dry-run: ${{ github.event_name == 'workflow_dispatch' && inputs.dry-run || 'false' }}
          github-token: ${{ steps.app.outputs.token }}
```

| Input | Default | Notes |
|---|---|---|
| `org` | repo owner | Org login to reconcile |
| `dry-run` | `true` | `false` to apply changes |
| `github-token` | — (required) | App installation token; needs `administration:write` + `contents/workflows:write` org-wide for enforce |

`GITHUB_TOKEN` alone can't do cross-repo org writes — mint an **App installation
token** (as above). See [Setup](#setup) for the App's permissions.

**As a reference implementation** — copy `src/reconcile_rulesets.py` + the
`workflows/` into your `.github` repo and adapt the constants. This is the shape
the sections below describe; the Action just bundles it behind `action.yml`.

---

## The core idea: FLOOR vs REVIEW

The trap in automating org-wide required reviews is the **solo-owner deadlock**:
if you require 1 approval on a one-person org, the owner's own PR can never be
approved, and the repo is bricked.

`gh-org-guard` solves this with two baselines and a strict ordering:

```
                    ┌─────────────────────────────────────────┐
                    │  Does the repo have BOTH:                │
                    │   • CODEOWNERS                           │
                    │   • the auto-approve caller workflow     │
                    └───────────────┬─────────────────────────┘
                       no │                    │ yes
                          ▼                    ▼
                   ┌────────────┐       ┌──────────────┐
                   │   FLOOR    │       │    REVIEW     │
                   │ 0 approvals│       │ 1 approval +  │
                   │ PR + no    │       │ code-owner,   │
                   │ force-push │       │ auto-approved │
                   └────────────┘       └──────────────┘
             always mergeable         owner's PR is auto-approved
             by the owner             by a bot-backed write user
```

- **FLOOR** — block force-push + deletion, require a PR + thread resolution, **0
  approvals**. Safe on *any* repo; can never deadlock.
- **REVIEW** — FLOOR plus 1 approval + code-owner review. Applied **only** once a
  working auto-approve caller exists, so the owner's own PRs get approved
  automatically and nothing locks.

A repo that can't receive the scaffolding files (e.g. missing write permission)
**stays at FLOOR** — never REVIEW — so it is always mergeable by its owner. The
reconciler even drops a REVIEW repo *back* to FLOOR temporarily if it needs to
write a missing file, then raises it again. Deadlock-safe by construction.

---

## The auto-approve trick (two identities)

GitHub has a hard rule most people hit here: **an App/bot review does not count**
toward a ruleset's required-approval count — only a review from a *user with write
access* does. So the reusable auto-approve workflow uses **two identities**:

- a **GitHub App token** to *read* team membership (is the PR author an admin?), and
- a **real write-collaborator PAT** to *cast* the approval (so it actually counts).

It only ever **approves** — it never merges. You keep the final "Merge when ready"
click. It also approves **once per PR** (not once per push), and refuses to
self-approve when the token user is the PR author.

---

## Repository layout

```
gh-org-guard/
├── action.yml                    # composite action: `uses: Aswincloud/gh-org-guard@v1`
├── src/
│   └── reconcile_rulesets.py     # the reconciler (~300 lines, env-driven)
├── workflows/
│   ├── reconcile-rulesets.yml    # weekly cron; enforces on schedule, dry-run on manual
│   ├── auto-approve.yml          # reusable auto-approve (workflow_call)
│   └── security-sweep.yml        # weekly secret-hygiene audit -> tracking issue
├── templates/
│   ├── CODEOWNERS.tmpl           # scaffolded into each repo
│   └── auto-approve-caller.yml.tmpl
├── examples/
│   └── org-guard.yml             # every knob documented in one place
└── README.md
```

> The `workflows/` files live at `.github/workflows/` in your actual org's
> `.github` repo (or any repo you run this from). They're kept in `workflows/`
> here so the whole toolkit reads top-to-bottom in one directory.

---

## Setup

1. **Create a GitHub App** for the org with these permissions and install it org-wide:
   - `administration: write` (write rulesets)
   - `contents: write` + `workflows: write` (scaffold CODEOWNERS + the caller)
   - `members: read` (auto-approve team lookups)
   - Save its **App ID** and a **private key** as repo/org secrets `APP_ID` and
     `APP_PRIVATE_KEY`.
2. **Add a write-collaborator PAT** as secret `WRITE_ACCESS_PAT` — a real user
   account (or a machine user) whose approvals count toward required reviews.
3. **Create two teams**: `admins` (humans whose PRs auto-approve) and
   `codeowners-bypass` (the approver identity), and put them in your CODEOWNERS.
4. **Drop the workflows** into your org's `.github` repo under `.github/workflows/`,
   and `src/reconcile_rulesets.py` alongside.
5. **Run it in dry-run first**: trigger *Reconcile org baseline rulesets* manually
   (it defaults to dry-run) and read the report. Then let the schedule enforce.

Optional: add `ORG_SECURITY_TOKEN` (a PAT with `admin:org` + `security_events`) so
the security sweep can read org-level security fields.

---

## Dry-run first, always

`reconcile_rulesets.py` defaults to `DRY_RUN=true` and changes **nothing** — it
prints a table of what it *would* do:

```
### org reconcile — DRY-RUN (no changes)
| repo | action | note |
|---|---|---|
| blog        | in-sync |  |
| new-service | FLOOR   | would add CODEOWNERS; would add caller |
| api         | REVIEW  | raised to require-review |

**review=1 floor=1 in-sync=1 skip=4 blocked=0 error=0**
```

Scheduled runs enforce; manual runs stay dry-run unless you flip the input. A
misclick can't rewrite the whole org.

---

## Design notes worth stealing

- **Reconcile, don't configure.** State drifts; a weekly PUT that reasserts the
  desired state is the only thing that stays true.
- **Break-glass is mandatory.** Any system that can lock a branch must leave one
  door open (here: org-admin always-bypass), or one bad required check bricks the repo.
- **Least-privilege secret passing.** Callers pass *only* the three secrets the
  reusable workflow declares — never `secrets: inherit`, which would leak every
  org secret into a workflow that shouldn't see them.
- **Pin third-party actions by SHA**, not tag — a moved tag is a supply-chain risk.
- **Idempotent everything.** Every run of every step is safe to repeat; "already
  correct" is a no-op, not an error.

---

## License

MIT — see [LICENSE](LICENSE). Copy freely; adapt the constants to your org.
