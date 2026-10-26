"""The `reviews-sync` job (§18.2 step 9): review entries after merges.

Merges done by auto-merge are made with GITHUB_TOKEN, and events caused by
GITHUB_TOKEN start no workflow, so nothing fires on merge. This job runs on
a schedule (every 10 minutes) and on demand. It scans merged pull requests
since the newest review entry (minus a 2-day overlap), oldest first; for
each one that changed exactly one `apps/<slug>.json` (not a delete) it
reads the row as the merge left it, and when `reviews/<slug>.json` has no
entry for that row's commit it appends one built from the PR's fused-review
block — the BOT's comment only, for the PR's head:

    {"commit", "at", "whats_new", "new_powers": [str], "credits": [login],
     "pr": int, "first": bool}          (newest first)

`credits` = authors of pull requests merged into the app's repo between the
previous approved commit and this one (compare → commits → PRs by sha; best
effort, empty when unknown). Each entry is one bot commit
"review: <slug> @ <sha7>", pushed to main (the ruleset's bypass list holds
the GitHub Actions app for exactly this).

A PR without a usable block is skipped (logged); the catalog serves that
row with `whats_new: null` until someone fixes it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check  # noqa: E402
from github import GitHub, GitHubError  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OVERLAP = dt.timedelta(days=2)
MAX_CREDIT_COMMITS = 40


def load_reviews(root: str, slug: str) -> list:
    path = os.path.join(root, "reviews", f"{slug}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    return data if isinstance(data, list) else []


def since(root: str) -> str:
    """ISO date to search from: the newest entry's `at` minus the overlap."""
    newest = None
    rdir = os.path.join(root, "reviews")
    for name in os.listdir(rdir) if os.path.isdir(rdir) else []:
        if not name.endswith(".json"):
            continue
        for e in load_reviews(root, name[:-5]):
            at = e.get("at") if isinstance(e, dict) else None
            if isinstance(at, str) and (newest is None or at > newest):
                newest = at
    if newest is None:
        return "2026-01-01"
    try:
        t = dt.datetime.fromisoformat(newest.replace("Z", "+00:00"))
    except ValueError:
        return "2026-01-01"
    return (t - OVERLAP).date().isoformat()


def entry_for(*, row: dict, pr: dict, block: dict, previous: list, credits: list) -> dict:
    return {"commit": row["commit"], "at": pr.get("merged_at"),
            "whats_new": block.get("whats_new") or "",
            "new_powers": [p.get("detail") for p in block.get("new_powers") or []
                           if isinstance(p, dict) and p.get("detail")],
            "credits": credits, "pr": pr["number"], "first": not previous}


def credits_for(gh: GitHub, repo: str, prev: str | None, commit: str) -> list:
    """Logins of PR authors merged into `repo` between `prev` and `commit`."""
    if not prev or prev == commit:
        return []
    owner = repo.split("/", 1)[0].lower()
    try:
        data = gh.compare(repo, prev, commit) or {}
    except GitHubError:
        return []
    logins: set = set()
    for c in (data.get("commits") or [])[-MAX_CREDIT_COMMITS:]:
        try:
            pulls = gh.commit_pulls(repo, c["sha"])
        except (GitHubError, KeyError):
            continue
        for p in pulls:
            login = ((p.get("user") or {}).get("login") or "")
            if p.get("merged_at") and login and login.lower() != owner \
                    and not login.endswith("[bot]"):
                logins.add(login)
    return sorted(logins)


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", ROOT, *args], check=True, capture_output=True,
                          text=True).stdout


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    gh = GitHub()
    query = f"repo:{repo} is:pr is:merged base:main merged:>={since(ROOT)}"
    # Oldest merge first, so entries stack newest-first in merge order.
    prs = sorted(gh.search_issues(query), key=lambda i: i.get("closed_at") or "")
    added = 0
    for item in prs:
        number = item["number"]
        try:
            pr = gh.pull(repo, number)
            files = gh.pull_files(repo, number)
        except GitHubError as e:
            print(f"#{number}: {e}", file=sys.stderr)
            continue
        rows = [f for f in files if check.ROW_PATH.match(f.get("filename", ""))]
        if len(rows) != 1 or len(files) != 1 or rows[0].get("status") == "removed":
            continue
        slug = check.ROW_PATH.match(rows[0]["filename"]).group(1)
        text = gh.file_text(repo, rows[0]["filename"], pr.get("merge_commit_sha") or "main")
        try:
            row = json.loads(text) if text else None
        except ValueError:
            row = None
        if not isinstance(row, dict) or not check.SHA_RE.match(str(row.get("commit", ""))):
            print(f"#{number}: no readable row for {slug}")
            continue
        previous = load_reviews(ROOT, slug)
        if any(isinstance(e, dict) and e.get("commit") == row["commit"] for e in previous):
            continue
        bot = check.find_review_comment(gh, repo, number)
        block = check.parse_block((bot or {}).get("body") or "")
        if not block or block.get("sha") != (pr.get("head") or {}).get("sha") or \
                block.get("status") not in ("pass", "needs-confirmation"):
            print(f"#{number}: no passing review block for its head; skipped")
            continue
        prev_commit = previous[0].get("commit") if previous else None
        entry = entry_for(row=row, pr=pr, block=block, previous=previous,
                          credits=credits_for(gh, row["repo"], prev_commit, row["commit"]))
        os.makedirs(os.path.join(ROOT, "reviews"), exist_ok=True)
        path = os.path.join(ROOT, "reviews", f"{slug}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([entry] + previous, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        git("add", path)
        git("commit", "-q", "-m", f"review: {slug} @ {row['commit'][:7]}")
        added += 1
        print(f"#{number}: review entry for {slug} @ {row['commit'][:7]}")
    if added:
        git("pull", "--rebase", "-q", "origin", "main")
        git("push", "-q", "origin", "HEAD:main")
    print(f"{added} review entr{'y' if added == 1 else 'ies'} added")
    return 0


if __name__ == "__main__":
    sys.exit(main())
