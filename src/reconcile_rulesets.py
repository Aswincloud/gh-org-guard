#!/usr/bin/env python3
"""Reconcile org standards across PUBLIC, ACTIVE repos: baseline ruleset + the
CODEOWNERS and auto-approve caller files.

Two baselines:
  FLOOR       — block force-push + deletion, require PR + thread resolution,
                0 approvals. Safe on any repo; can't deadlock a solo owner.
  REVIEW      — FLOOR plus 1 approval + code-owner review. Only safe once the
                repo has a working auto-approve caller (else the owner's own PR
                can't be approved -> deadlock).

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


def rules(review):
    pr = {
        "required_approving_review_count": 1 if review else 0,
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": bool(review),
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


def payload(review):
    return {"name": BASELINE, "target": "branch", "enforcement": "active",
            "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
            # Break-glass: org admins can bypass (direct/admin-merge) so a
            # misconfigured required check + merge queue can never permanently
            # lock a repo. Managed here so the weekly PUT preserves it.
            "bypass_actors": [{"actor_id": 1, "actor_type": "OrganizationAdmin",
                               "bypass_mode": "always"}],
            "rules": rules(review)}


MANAGED = {"deletion": [], "non_fast_forward": [],
           "pull_request": ["required_approving_review_count",
                            "require_code_owner_review",
                            "required_review_thread_resolution"],
           "merge_queue": ["merge_method", "grouping_strategy",
                           "min_entries_to_merge",
                           "min_entries_to_merge_wait_minutes"]}


def norm(rs, bypass=None):
    by = {r["type"]: (r.get("parameters") or {}) for r in rs}
    has_admin_bypass = any((b.get("actor_type") == "OrganizationAdmin")
                           for b in (bypass or []))
    return json.dumps({"rules": {t: ({k: by[t].get(k) for k in ks} if t in by else None)
                                 for t, ks in MANAGED.items()},
                       "admin_bypass": has_admin_bypass}, sort_keys=True)


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


def find_baseline(repo):
    r = gh(f"repos/{ORG}/{repo}/rulesets")
    if r.returncode != 0:
        return None, r.stderr.strip()
    for rs_ in json.loads(r.stdout or "[]"):
        if rs_.get("name") == BASELINE:
            return json.loads(gh(f"repos/{ORG}/{repo}/rulesets/{rs_['id']}").stdout), None
    return None, None


def set_baseline(repo, cur, review):
    body = payload(review)
    if cur is None:
        r = gh(f"repos/{ORG}/{repo}/rulesets", method="POST", body=body)
    else:
        r = gh(f"repos/{ORG}/{repo}/rulesets/{cur['id']}", method="PUT", body=body)
    if r.returncode == 0:
        return None
    e = r.stderr.lower()
    if "administration" in e or "not accessible" in e:
        return "needs App administration:write"
    return r.stderr.strip()[:70]


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
    tmpl_co = read_template(TMPL_CODEOWNERS)
    tmpl_caller = read_template(TMPL_CALLER)

    rows = []
    c = {"review": 0, "floor": 0, "in_sync": 0, "skip": 0, "blocked": 0, "error": 0}
    want_review = norm(rules(True), [{"actor_type": "OrganizationAdmin"}])

    for repo in repos:
        name = repo["name"]
        why = skip_reason(repo)
        if why:
            c["skip"] += 1
            rows.append((name, "skip", why))
            continue
        branch = repo["default_branch"]
        set_note = ensure_repo_settings(name)  # enable allow_auto_merge (idempotent)
        cur, err = find_baseline(name)
        if err:
            c["error"] += 1
            rows.append((name, "error", err[:50]))
            continue

        # Step 1: ensure files. If repo is at REVIEW but a file is missing, the
        # write could be blocked by protection — drop to FLOOR first.
        co_ok = any(has_file(name, p, branch) for p in CODEOWNERS_PATHS)
        caller_ok = has_file(name, CALLER_PATH, branch)
        if (not co_ok or not caller_ok) and cur is not None:
            curp = next((r for r in cur.get("rules", []) if r["type"] == "pull_request"), None)
            if curp and curp["parameters"].get("required_approving_review_count", 0) > 0:
                if not DRY_RUN:
                    set_baseline(name, cur, review=False)  # drop to FLOOR to allow writes
                    cur, _ = find_baseline(name)

        co_ok, caller_ok, fnote = ensure_files(name, branch, tmpl_co, tmpl_caller)
        if set_note:  # surface an auto-merge settings change in the report note
            fnote = f"{fnote}; {set_note}".strip("; ") if fnote else set_note

        # Step 2: choose target baseline by whether the auto-approver exists.
        target_review = caller_ok and co_ok
        problem = None
        if cur is None or norm(cur.get("rules", []), cur.get("bypass_actors")) != norm(rules(target_review), [{"actor_type": "OrganizationAdmin"}]) \
                or cur.get("enforcement") != "active":
            problem = set_baseline(name, cur, review=target_review)

        if problem:
            c["blocked"] += 1
            rows.append((name, "BLOCKED", problem))
        elif not target_review:
            c["floor"] += 1
            rows.append((name, "FLOOR", fnote or "no auto-approver yet -> floor only"))
        else:
            # is it already at review with files? classify sync vs changed
            was_review = cur is not None and norm(cur.get("rules", []), cur.get("bypass_actors")) == want_review
            noop = fnote in ("", "self")
            if was_review and noop:
                c["in_sync"] += 1
                rows.append((name, "in-sync", ""))
            else:
                c["review"] += 1
                rows.append((name, "REVIEW", fnote or "raised to require-review"))

    mode = "DRY-RUN (no changes)" if DRY_RUN else "ENFORCE"
    out = [f"### org reconcile — {mode}", "", "| repo | action | note |", "|---|---|---|"]
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
