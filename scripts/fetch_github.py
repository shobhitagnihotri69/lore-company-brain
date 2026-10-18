"""
Download 12 months of merged PRs and issues from kubernetes/kubernetes
for the 5 slice teams, using GitHub's GraphQL API.

What is GraphQL? A single API endpoint (POST https://api.github.com/graphql)
where we describe exactly which fields we want. One call returns up to 50 PRs
*with* their files, reviews and comments, instead of 3+ REST calls per PR.

Privacy: we keep only GitHub logins and public activity (numbers, titles,
labels, file paths, timestamps). No emails, no names, no PR descriptions,
no issue or comment bodies. Comment bodies are read only to set two flags
(/lgtm and /approve lines) and are then thrown away.

Cache: each (type, team, month) is saved to
    data/raw/<prs|issues>/<team>/<YYYY-MM>.json
If that file exists it is skipped, so re-running continues where it stopped.

Usage:
    python scripts/fetch_github.py                                # everything
    python scripts/fetch_github.py --teams sig-scheduling --months 2026-09
"""

import argparse
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ── config ──────────────────────────────────────────────────────────────
REPO = "kubernetes/kubernetes"
TEAMS = ["sig-node", "sig-scheduling", "sig-auth", "sig-storage", "sig-network"]
START = date(2025, 10, 18)   # first day included
END = date(2026, 10, 18)     # last day included
RAW = Path("data/raw")

GRAPHQL_URL = "https://api.github.com/graphql"
PAGE_SIZE = 50             # items per call
SMALLER_PAGES = (20, 10)   # after each timeout, step down to the next size
TIMEOUT_WAIT = 30          # seconds to wait after a timeout before retrying
MIN_REMAINING = 200        # below this many points left, sleep until the reset
MAX_TRIES = 5

TOKEN = os.environ.get("GITHUB_TOKEN", "")
if not TOKEN:
    raise SystemExit("GITHUB_TOKEN is missing. Put GITHUB_TOKEN=... in .env")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


# ── GraphQL queries ─────────────────────────────────────────────────────
# `search` returns at most 1000 results per query, so we query one month at a time.
PR_QUERY = """
query($q: String!, $n: Int!, $after: String) {
  rateLimit { remaining resetAt cost }
  search(query: $q, type: ISSUE, first: $n, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title createdAt mergedAt
        author { login }
        labels(first: 50) { nodes { name } }
        files(first: 100) { totalCount nodes { path } }
        reviews(first: 50) { totalCount nodes { author { login } state submittedAt } }
        comments(first: 100) { totalCount nodes { author { login } createdAt body } }
      }
    }
  }
}
"""

ISSUE_QUERY = """
query($q: String!, $n: Int!, $after: String) {
  rateLimit { remaining resetAt cost }
  search(query: $q, type: ISSUE, first: $n, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Issue {
        number title createdAt closedAt state
        author { login }
        labels(first: 50) { nodes { name } }
        assignees(first: 20) { nodes { login } }
        comments(first: 50) { totalCount nodes { author { login } createdAt } }
      }
    }
  }
}
"""


# ── helpers ─────────────────────────────────────────────────────────────
def login(actor):
    """Author can be null when the account was deleted."""
    return actor["login"] if actor else None


def has_line_starting(body, command):
    """True if any line of the comment starts with e.g. '/lgtm'."""
    return any(line.strip().startswith(command) for line in (body or "").splitlines())


def month_ranges():
    """Yield ("2025-10", first_day, last_day) for each month, clipped to START..END."""
    y, m = START.year, START.month
    while date(y, m, 1) <= END:
        first = date(y, m, 1)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        yield f"{y:04d}-{m:02d}", max(first, START), min(nxt - timedelta(days=1), END)
        y, m = nxt.year, nxt.month


def wait_for_rate_limit(rate):
    if rate["remaining"] < MIN_REMAINING:
        reset = datetime.fromisoformat(rate["resetAt"].replace("Z", "+00:00"))
        wait = max((reset - datetime.now(timezone.utc)).total_seconds() + 5, 1)
        print(f"    [rate limit] {rate['remaining']} points left, sleeping {wait:.0f}s", flush=True)
        time.sleep(wait)


class Timeout(Exception):
    """GitHub didn't give a full answer in time. The caller waits and retries with smaller pages."""


def post(query, variables):
    """One GraphQL call with retries. Returns the `data` part of the answer."""
    for attempt in range(1, MAX_TRIES + 1):
        try:
            resp = requests.post(GRAPHQL_URL, json={"query": query, "variables": variables},
                                 headers=HEADERS, timeout=90)
        except requests.exceptions.Timeout:
            raise Timeout("no answer within 90s")
        except (requests.exceptions.ConnectionError, requests.exceptions.ChunkedEncodingError) as e:
            raise Timeout(f"connection error: {e}")

        if resp.status_code == 504:
            raise Timeout("HTTP 504")
        text = resp.text.lower()
        if resp.status_code in (502, 503) or "secondary rate limit" in text:
            if resp.status_code == 502 and variables["n"] == PAGE_SIZE:
                raise Timeout("HTTP 502")  # probably too big a page
            print(f"    [retry {attempt}/{MAX_TRIES}] HTTP {resp.status_code}, waiting 60s", flush=True)
            time.sleep(60)
            continue
        resp.raise_for_status()

        try:
            body = resp.json()
        except requests.exceptions.JSONDecodeError as e:
            raise Timeout(f"cut-off answer ({e})")
        errors = body.get("errors") or []
        if any(e.get("type") == "RATE_LIMITED" for e in errors):
            print(f"    [retry {attempt}/{MAX_TRIES}] rate limited, waiting 60s", flush=True)
            time.sleep(60)
            continue
        if any("timeout" in (e.get("message") or "").lower() for e in errors):
            raise Timeout(errors[0]["message"])
        if errors:
            raise RuntimeError(f"GraphQL error: {errors}")
        return body["data"]
    raise RuntimeError(f"gave up after {MAX_TRIES} tries")


# ── turning API answers into our saved shape ────────────────────────────
def clean_pr(n):
    return {
        "number": n["number"],
        "title": n["title"],
        "author": login(n["author"]),
        "createdAt": n["createdAt"],
        "mergedAt": n["mergedAt"],
        "labels": [l["name"] for l in n["labels"]["nodes"]],
        "files": [f["path"] for f in n["files"]["nodes"]],
        "files_total": n["files"]["totalCount"],
        "reviews": [{"author": login(r["author"]), "state": r["state"],
                     "submittedAt": r["submittedAt"]} for r in n["reviews"]["nodes"]],
        "reviews_total": n["reviews"]["totalCount"],
        "comments": [{"author": login(c["author"]), "createdAt": c["createdAt"],
                      "lgtm": has_line_starting(c["body"], "/lgtm"),
                      "approve": has_line_starting(c["body"], "/approve")}
                     for c in n["comments"]["nodes"]],   # body is dropped here
        "comments_total": n["comments"]["totalCount"],
    }


def clean_issue(n):
    return {
        "number": n["number"],
        "title": n["title"],
        "author": login(n["author"]),
        "createdAt": n["createdAt"],
        "closedAt": n["closedAt"],
        "state": n["state"],
        "labels": [l["name"] for l in n["labels"]["nodes"]],
        "assignees": [a["login"] for a in n["assignees"]["nodes"]],
        "comments": [{"author": login(c["author"]), "createdAt": c["createdAt"]}
                     for c in n["comments"]["nodes"]],
        "comments_total": n["comments"]["totalCount"],
    }


# ── fetching one (type, team, month) ────────────────────────────────────
def fetch_bucket(kind, team, month, first, last):
    dest = RAW / f"{kind}s" / team / f"{month}.json"
    if dest.exists():
        print(f"  {kind:5} {team:15} {month}  cached, skipped", flush=True)
        return

    label = "sig/" + team[len("sig-"):]
    days = f"{first.isoformat()}..{last.isoformat()}"
    if kind == "pr":
        q = f"repo:{REPO} is:pr is:merged label:{label} merged:{days}"
        query, clean = PR_QUERY, clean_pr
    else:
        q = f"repo:{REPO} is:issue label:{label} created:{days}"
        query, clean = ISSUE_QUERY, clean_issue

    items, after, n, calls, left, total = [], None, PAGE_SIZE, 0, None, 0
    tries = 0  # failed tries for the current page
    while True:
        try:
            data = post(query, {"q": q, "n": n, "after": after})
        except Timeout as e:
            tries += 1
            if tries >= MAX_TRIES:
                raise RuntimeError(f"gave up after {MAX_TRIES} tries: {e}") from e
            n = next((s for s in SMALLER_PAGES if s < n), n)  # 50 -> 20 -> 10, then stays 10
            print(f"    [timeout {tries}/{MAX_TRIES}] {e}; waiting {TIMEOUT_WAIT}s, "
                  f"then pages of {n}", flush=True)
            time.sleep(TIMEOUT_WAIT)
            continue
        tries = 0
        calls += 1
        rate, search = data["rateLimit"], data["search"]
        left, total = rate["remaining"], search["issueCount"]
        items += [clean(node) for node in search["nodes"] if node]
        wait_for_rate_limit(rate)
        if not search["pageInfo"]["hasNextPage"]:
            break
        after = search["pageInfo"]["endCursor"]

    if total > 1000:
        print(f"    WARNING: {total} results but search stops at 1000; split this month", flush=True)

    # Write to a temp file first, then rename: a half-written file is never left behind
    # (it would otherwise be "cached" and skipped forever).
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps({
        "type": kind, "team": team, "month": month, "range": days, "search": q,
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(items), "items": items,
    }, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dest)
    print(f"  {kind:5} {team:15} {month}  {len(items):4} items  "
          f"({calls} calls, {left} points left)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", nargs="+", default=TEAMS, choices=TEAMS)
    ap.add_argument("--months", nargs="+", help="e.g. 2026-09 (default: all 12 months)")
    args = ap.parse_args()

    months = [m for m in month_ranges() if not args.months or m[0] in args.months]
    started = time.time()
    for team in args.teams:
        for month, first, last in months:
            for kind in ("pr", "issue"):
                fetch_bucket(kind, team, month, first, last)
    print(f"Done in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
