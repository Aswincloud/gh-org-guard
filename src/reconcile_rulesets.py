#!/usr/bin/env python3
"""Reconcile org standards across PUBLIC, ACTIVE repos: baseline ruleset + the
CODEOWNERS and auto-approve caller files.

Two rulesets, deliberately split:
  org-baseline          — block force-push + deletion, require a PR + thread
                          resolution, and the MERGE QUEUE. 0 approvals, no
                          code-owner gate. Org admins keep a break-glass bypass
                          so a bad required check can't brick a repo.
  org-codeowner-review  — 1 approval + code-owner review. Bypassable by
                          @ORG/<BYPASS_TEAM> with bypass_mode "exempt", so the
                          rule is NOT APPLICABLE to them: their PRs read CLEAN
                          with zero approvals and take the normal green
                          "Merge when ready" path through the queue.
                          Do not change that mode to "always" or
                          "pull_request" — those only grant a force-merge that
                          SKIPS the queue. See codeowner_payload().

Why two rulesets and not one: rules from all matching rulesets aggregate to the
MOST RESTRICTIVE value. If the baseline also asserted require_code_owner_review
then bypassing the code-owner ruleset would be cancelled out by the baseline's
own copy, and the bypass would silently do nothing. The approval + code-owner
rules must live ONLY in the bypassable ruleset.

Two tiers, as before:
  FLOOR       — org-baseline only. Safe on any repo; can't deadlock an owner.
  REVIEW      — FLOOR plus org-codeowner-review. Only applied once the repo has
                a working auto-approve caller + CODEOWNERS.

Deadlock-safe ordering per repo:
  1. Ensure files (CODEOWNERS + caller). Writing needs the branch to accept a
     commit, so if a repo is already at REVIEW but missing the caller, drop it
     to FLOOR first, write, then raise.
  2. Only apply REVIEW once BOTH files are confirmed present.
A repo that can't get files (e.g. write perms missing) stays at FLOOR — never
REVIEW — so it is always mergeable by its owner.

Writing files needs the token to hold contents:write + workflows:write. Without
them the file step reports 'needs App contents/workflows:write' and the repo is
held at FLOOR (not deadlocked).

DRY_RUN=true (default) changes nothing.
"""
import base64
import json
import os
import subprocess
import sys

ORG = os.environ.get("ORG")
if not ORG:
    sys.exit("ORG env var is required (the GitHub org login to reconcile).")
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
# When set (the GitHub Action sets it to the action's bundled templates/ dir),
# templates load from local disk instead of round-tripping to the org's .github
# repo. Placeholder org logins in the templates are substituted for the real org.
TEMPLATES_DIR = os.environ.get("TEMPLATES_DIR")
BASELINE = "org-baseline"
CODEOWNER_RS = "org-codeowner-review"
# Members of this team bypass org-codeowner-review (and only that ruleset).
BYPASS_TEAM = os.environ.get("BYPASS_TEAM", "admins")
SELF = ".github"  # this repo hosts templates + the reusable workflow

CODEOWNERS_PATHS = [".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"]
CALLER_PATH = ".github/workflows/auto-approve.yml"
TMPL_CODEOWNERS = ".github/templates/CODEOWNERS.tmpl"
TMPL_CALLER = ".github/templates/auto-approve-caller.yml.tmpl"


def gh(path, *extra, method=None, body=None):
    args = ["gh", "api"]
    if method:
        args += ["-X", method]
    args.append(path)
    args += list(extra)
    if body is not None:
        args += ["--input", "-"]
    return subprocess.run(args, capture_output=True, text=True,
                          input=(json.dumps(body) if body is not None else None))


def baseline_rules():
    """The floor that binds EVERYONE — no bypass actors. Deliberately carries no
    approval count and no code-owner gate; those live in codeowner_rules() so a
    team can be exempted from them without also escaping the merge queue."""
    pr = {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": False,
        "require_last_push_approval": False,
        "required_review_thread_resolution": True,
    }
    mq = {
        "check_response_timeout_minutes": 60,
        "grouping_strategy": "ALLGREEN",
        "max_entries_to_build": 5,
        "max_entries_to_merge": 5,
        "merge_method": "SQUASH",
        "min_entries_to_merge": 1,
        # Solo org: don't wait to batch entries — a lone PR merges immediately.
        "min_entries_to_merge_wait_minutes": 0,
    }
    return [{"type": "deletion"}, {"type": "non_fast_forward"},
            {"type": "pull_request", "parameters": pr},
            {"type": "merge_queue", "parameters": mq}]


def codeowner_rules():
    """The review gate, bypassable by @ORG/BYPASS_TEAM. MUST NOT be duplicated
    into baseline_rules() — see the module docstring on rule aggregation."""
    return [{"type": "pull_request", "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": True,
        "require_last_push_approval": False,
        "required_review_thread_resolution": True,
    }}]


def baseline_payload():
    # Break-glass: org admins can bypass, so a misconfigured required check or a
    # jammed merge queue can never permanently lock a repo.
    #
    # Kept even though the bypass team now reaches the queue cleanly via
    # org-codeowner-review's "exempt" actor: that exemption covers the REVIEW
    # gate only. If a required status check or the queue itself jams, this is
    # the sole escape hatch, and removing it once already left 15 repos with no
    # way to merge anything at all.
    return {"name": BASELINE, "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "bypass_actors": [{"actor_id": 1, "actor_type": "OrganizationAdmin",
                               "bypass_mode": "always"}],
            "rules": baseline_rules()}


def codeowner_payload(team):
    # bypass_mode MUST be "exempt", not "always" or "pull_request".
    #
    # The three modes are not degrees of the same thing:
    #   always / pull_request -> the actor may OVERRIDE the rule at merge time.
    #       GitHub offers "merge without waiting for requirements (bypass rules)",
    #       which merges directly and SKIPS THE MERGE QUEUE. It never lights up
    #       "Merge when ready", because queue entry is gated on the pull request
    #       satisfying the rules, and an override is not satisfaction.
    #   exempt -> the rule is treated as NOT APPLICABLE to the actor. The PR is
    #       simply CLEAN for them, so the green "Merge when ready" appears and the
    #       PR goes through the queue like anyone else's.
    #
    # Verified on live PRs: always -> BLOCKED, pull_request -> BLOCKED,
    # exempt -> CLEAN with zero approvals while require_code_owner_review stays on.
    #
    # Only the team is listed. OrganizationAdmin is deliberately absent: an
    # "always" actor here would re-offer the force-merge path, and break-glass
    # belongs on org-baseline (which owns the queue), not on the review gate.
    return {"name": CODEOWNER_RS, "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            "bypass_actors": [
                {"actor_id": team, "actor_type": "Team", "bypass_mode": "exempt"},
            ],
            "rules": codeowner_rules()}


MANAGED_BASELINE = {"deletion": [], "non_fast_forward": [],
                    "pull_request": ["required_approving_review_count",
                                     "require_code_owner_review",
                                     "required_review_thread_resolution"],
                    "merge_queue": ["merge_method", "grouping_strategy",
                                    "min_entries_to_merge",
                                    "min_entries_to_merge_wait_minutes"]}

MANAGED_CODEOWNER = {"pull_request": ["required_approving_review_count",
                                      "require_code_owner_review"]}


def _actor_key(b):
    """Canonical bypass-actor identity for comparison.

    GitHub echoes actor_id=null for OrganizationAdmin on READ but requires
    actor_id=1 on WRITE. Comparing raw makes a correctly-written ruleset look
    like drift forever, so pin that one actor's id.
    """
    t = b.get("actor_type")
    aid = 1 if t == "OrganizationAdmin" else b.get("actor_id")
    return "{}:{}:{}".format(t, aid, b.get("bypass_mode"))


def norm(rs, managed, bypass=None):
    """Comparable fingerprint of the rules we manage plus the exact bypass list.
    Bypass is compared verbatim (not just 'has an admin') because the whole
    point of the split is WHO is on the codeowner ruleset's bypass list."""
    by = {r["type"]: (r.get("parameters") or {}) for r in rs}
    actors = sorted(_actor_key(b) for b in (bypass or []))
    return json.dumps({"rules": {t: ({k: by[t].get(k) for k in ks} if t in by else None)
                                 for t, ks in managed.items()},
                       "bypass": actors}, sort_keys=True)


def team_id(slug):
    r = gh(f"orgs/{ORG}/teams/{slug}")
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout).get("id")
    except (ValueError, AttributeError):
        return None


def skip_reason(repo):
    if repo["private"]:
        return "private (needs Pro/Team)"
    if repo["archived"]:
        return "archived"
    if repo["fork"]:
        return "fork"
    if repo.get("is_template"):
        return "template"
    if not repo.get("default_branch"):
        return "empty"
    return None


def find_ruleset(repo, name):
    r = gh(f"repos/{ORG}/{repo}/rulesets")
    if r.returncode != 0:
        return None, r.stderr.strip()
    for rs_ in json.loads(r.stdout or "[]"):
        if rs_.get("name") == name:
            return json.loads(gh(f"repos/{ORG}/{repo}/rulesets/{rs_['id']}").stdout), None
    return None, None


def _rs_err(r):
    e = r.stderr.lower()
    if "administration" in e or "not accessible" in e:
        return "needs App administration:write"
    return r.stderr.strip()[:70]


def put_ruleset(repo, cur, body):
    # NOTE: pre-split, ruleset writes bypassed the DRY_RUN gate entirely — the
    # module docstring promised "changes nothing" but rulesets were still
    # POST/PUT'd. Guard both write paths at the choke point.
    if DRY_RUN:
        return None
    if cur is None:
        r = gh(f"repos/{ORG}/{repo}/rulesets", method="POST", body=body)
    else:
        r = gh(f"repos/{ORG}/{repo}/rulesets/{cur['id']}", method="PUT", body=body)
    return None if r.returncode == 0 else _rs_err(r)


def delete_ruleset(repo, cur):
    if cur is None:
        return None
    if DRY_RUN:
        return None
    if DRY_RUN:
        return None
    r = gh(f"repos/{ORG}/{repo}/rulesets/{cur['id']}", method="DELETE")
    return None if r.returncode == 0 else _rs_err(r)


def has_file(repo, path, branch):
    return gh(f"repos/{ORG}/{repo}/contents/{path}?ref={branch}").returncode == 0


def read_template(path):
    # Action mode: read the bundled template from local disk (path's basename),
    # substituting the YOUR_ORG placeholder for the real org login.
    if TEMPLATES_DIR:
        local = os.path.join(TEMPLATES_DIR, os.path.basename(path))
        try:
            with open(local, encoding="utf-8") as fh:
                return fh.read().replace("YOUR_ORG", ORG)
        except OSError:
            return None
    # Reference-impl mode: read from the org's .github repo over the API.
    r = gh(f"repos/{ORG}/{SELF}/contents/{path}?ref=main")
    if r.returncode != 0:
        return None
    return base64.b64decode(json.loads(r.stdout)["content"]).decode()


def put_file(repo, path, content, branch, msg):
    b64 = base64.b64encode(content.encode()).decode()
    args = ["-f", f"message={msg}", "-f", f"content={b64}", "-f", f"branch={branch}"]
    r = gh(f"repos/{ORG}/{repo}/contents/{path}", *args, method="PUT")
    if r.returncode == 0:
        return None
    e = r.stderr.lower()
    if "workflow" in e and "scope" in e or "refusing" in e or "not accessible" in e:
        return "needs App contents/workflows:write"
    if "rule violations" in e:
        return "branch protected (floor not applied first?)"
    return r.stderr.strip()[:70]


def ensure_repo_settings(repo):
    """Idempotently enable allow_auto_merge so PRs can use the merge queue /
    'Merge when ready'. Returns a short note if it changed something, else ''."""
    r = gh(f"repos/{ORG}/{repo}")
    if r.returncode != 0:
        return ""
    try:
        cur = json.loads(r.stdout).get("allow_auto_merge")
    except Exception:
        cur = None
    if cur is True:
        return ""
    if DRY_RUN:
        return "would enable auto-merge"
    w = gh(f"repos/{ORG}/{repo}", "-F", "allow_auto_merge=true", method="PATCH")
    return "auto-merge on" if w.returncode == 0 else "auto-merge FAILED"


def ensure_files(repo, branch, tmpl_co, tmpl_caller):
    """Return (co_ok, caller_ok, note). Writes missing files to the default branch."""
    co_ok = any(has_file(repo, p, branch) for p in CODEOWNERS_PATHS)
    caller_ok = has_file(repo, CALLER_PATH, branch)
    if repo == SELF:
        # .github hosts the reusable definition at CALLER_PATH; its caller is a
        # different filename. Treat its own files as managed elsewhere.
        return co_ok, True, "self"
    notes = []
    if not co_ok and tmpl_co is not None:
        if not DRY_RUN:
            n = put_file(repo, ".github/CODEOWNERS", tmpl_co, branch, "chore: add CODEOWNERS (reconciler)")
            if n:
                notes.append(f"CODEOWNERS:{n}")
            else:
                co_ok = True
        else:
            notes.append("would add CODEOWNERS")
    if not caller_ok and tmpl_caller is not None:
        if not DRY_RUN:
            n = put_file(repo, CALLER_PATH, tmpl_caller, branch, "ci: add auto-approve caller (reconciler)")
            if n:
                notes.append(f"caller:{n}")
            else:
                caller_ok = True
        else:
            notes.append("would add caller")
    return co_ok, caller_ok, "; ".join(notes)


def main():
    r = gh(f"orgs/{ORG}/repos", "--paginate", "--jq",
           ".[] | {name, private, archived, fork, is_template, default_branch}")
    if r.returncode != 0:
        print(f"repo list failed: {r.stderr}", file=sys.stderr)
        sys.exit(2)
    repos = sorted((json.loads(x) for x in r.stdout.splitlines() if x.strip()),
                   key=lambda x: x["name"])

    team = team_id(BYPASS_TEAM)
    if team is None:
        print(f"cannot resolve @{ORG}/{BYPASS_TEAM} — refusing to write a "
              f"codeowner ruleset with no bypass list", file=sys.stderr)
        sys.exit(2)
    print(f"bypass team: @{ORG}/{BYPASS_TEAM} (id={team})", file=sys.stderr)

    tmpl_co = read_template(TMPL_CODEOWNERS)
    tmpl_caller = read_template(TMPL_CALLER)

    want_base = norm(baseline_rules(), MANAGED_BASELINE,
                     baseline_payload()["bypass_actors"])
    want_co = norm(codeowner_rules(), MANAGED_CODEOWNER,
                   codeowner_payload(team)["bypass_actors"])

    rows = []
    c = {"review": 0, "floor": 0, "in_sync": 0, "skip": 0, "blocked": 0, "error": 0}

    for repo in repos:
        name = repo["name"]
        why = skip_reason(repo)
        if why:
            c["skip"] += 1
            rows.append((name, "skip", why))
            continue
        branch = repo["default_branch"]
        set_note = ensure_repo_settings(name)  # enable allow_auto_merge (idempotent)

        base, err = find_ruleset(name, BASELINE)
        if err:
            c["error"] += 1
            rows.append((name, "error", err[:50]))
            continue
        cors, err = find_ruleset(name, CODEOWNER_RS)
        if err:
            c["error"] += 1
            rows.append((name, "error", err[:50]))
            continue

        # Step 1: ensure files. The approval gate lives in CODEOWNER_RS, so that
        # is the ruleset that can block a direct write — drop it first if a file
        # is missing, then re-add below once both files are present.
        co_ok = any(has_file(name, p, branch) for p in CODEOWNERS_PATHS)
        caller_ok = has_file(name, CALLER_PATH, branch)
        if (not co_ok or not caller_ok) and cors is not None and not DRY_RUN:
            delete_ruleset(name, cors)
            cors = None

        co_ok, caller_ok, fnote = ensure_files(name, branch, tmpl_co, tmpl_caller)
        if set_note:  # surface an auto-merge settings change in the report note
            fnote = f"{fnote}; {set_note}".strip("; ") if fnote else set_note

        # Step 2: baseline always; codeowner ruleset only once the auto-approver
        # exists (otherwise a non-bypass member's PR could never be approved).
        target_review = caller_ok and co_ok
        was_base = base is not None and \
            norm(base.get("rules", []), MANAGED_BASELINE, base.get("bypass_actors")) == want_base \
            and base.get("enforcement") == "active"
        was_co = cors is not None and \
            norm(cors.get("rules", []), MANAGED_CODEOWNER, cors.get("bypass_actors")) == want_co \
            and cors.get("enforcement") == "active"

        problem = None
        if not was_base:
            problem = put_ruleset(name, base, baseline_payload())
        if not problem:
            if target_review and not was_co:
                problem = put_ruleset(name, cors, codeowner_payload(team))
            elif not target_review and cors is not None:
                problem = delete_ruleset(name, cors)

        if problem:
            c["blocked"] += 1
            rows.append((name, "BLOCKED", problem))
        elif not target_review:
            c["floor"] += 1
            rows.append((name, "FLOOR", fnote or "no auto-approver yet -> floor only"))
        elif was_base and was_co and fnote in ("", "self"):
            c["in_sync"] += 1
            rows.append((name, "in-sync", ""))
        else:
            c["review"] += 1
            rows.append((name, "REVIEW", fnote or "raised to require-review"))

    mode = "DRY-RUN (no changes)" if DRY_RUN else "ENFORCE"
    out = [f"### org reconcile — {mode}", "",
           f"`{BASELINE}` (no bypass) + `{CODEOWNER_RS}` "
           f"(bypass: @{ORG}/{BYPASS_TEAM})", "",
           "| repo | action | note |", "|---|---|---|"]
    out += [f"| {n} | {a} | {x} |" for n, a, x in rows]
    out += ["", f"**review={c['review']} floor={c['floor']} in-sync={c['in_sync']} "
            f"skip={c['skip']} blocked={c['blocked']} error={c['error']}**"]
    report = "\n".join(out)
    print(report)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        open(summ, "a", encoding="utf-8").write(report + "\n")
    if c["error"] or c["blocked"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
