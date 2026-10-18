"""Load the downloaded GitHub PRs and issues into the existing 'k8s' graph.

Run after load_k8s_brain.py. This script only ADDS to the graph (MERGE):
it never deletes the graph or the KEP data, so running it twice is safe.

    FALKORDB_HOST=localhost python scripts/load_github.py

Reads data/raw/prs/<team>/*.json and data/raw/issues/<team>/*.json
(written by fetch_github.py).

Every login is lowercased, then fixed with data/aliases.yaml, and bots are dropped.
A Person already in the graph under a misspelled login (from the KEP loader) is
merged into the right login.

Adds:
  (:PR {num, title, created, merged})
  (:Issue {num, title, created, closed, state})
  (:Folder {path})                          first 3 folder levels of a changed file
                                            (under staging/src/: 3 levels after that)
  (Person)-[:AUTHORED {at}]->(PR|Issue)
  (Person)-[:REVIEWED {at, state}]->(PR)    one per person per PR
  (Person)-[:LGTM {at}]->(PR)               a PR comment had a /lgtm line
  (Person)-[:APPROVED {at}]->(PR)           a PR comment had an /approve line
  (Person)-[:COMMENTED {at}]->(Issue)       one per person per issue
  (Person)-[:ASSIGNED]->(Issue)
  (PR|Issue)-[:FOR_TEAM]->(Team)            the team folder the item was downloaded under
  (PR)-[:TOUCHES]->(Folder)
Edges with an `at` keep the person's EARLIEST date for that PR or issue.
"""
import json
import os
import time
from collections import Counter
from pathlib import Path

from falkordb import FalkorDB

from logins import ALIASES as aliases, is_bot

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
TEAMS = ["sig-node", "sig-scheduling", "sig-auth", "sig-storage", "sig-network"]
# A person can review one PR many times. REVIEWED.state keeps their strongest verdict.
STATE_RANK = {"APPROVED": 3, "CHANGES_REQUESTED": 2, "COMMENTED": 1}

stats = Counter()
display = {}  # login -> spelling to show (GitHub's own capitals)


def person(raw):
    """GitHub login -> our Person key (same rule as logins.clean_login, plus counts).
    None for deleted accounts and bots."""
    if not raw:
        return None
    login = raw.lower()
    if login in aliases:
        login = aliases[login]
        stats["alias fixes"] += 1
    if is_bot(login):
        stats["bot logins skipped"] += 1
        return None
    display.setdefault(login, raw if raw.lower() == login else login)
    return login


def folder(path):
    """First 3 folder levels: 'pkg/scheduler/framework/plugins/x.go' -> 'pkg/scheduler/framework'.
    staging/src/ holds whole libraries, so there we count 3 levels after 'staging/src/'
    (else every staging file lands in one folder): -> 'staging/src/k8s.io/kubelet/pkg'.
    Files in the repo root (go.mod, ...) have no folder -> None."""
    parts = path.split("/")[:-1]  # drop the file name
    keep = 5 if parts[:2] == ["staging", "src"] else 3
    return "/".join(parts[:keep]) or None


def keep_earliest(edges, key, at):
    if at and (key not in edges or at < edges[key]):
        edges[key] = at


def read(kind):
    """Read every downloaded file of one kind ('prs' or 'issues').
    An item labelled with two teams is in two files: keep the most recently
    downloaded copy, and remember every team it was downloaded for."""
    items, teams, fetched, files = {}, {}, {}, 0
    for f in sorted(RAW.glob(f"{kind}/*/*.json")):
        files += 1
        data = json.loads(f.read_text(encoding="utf-8"))
        for it in data["items"]:
            n = it["number"]
            teams.setdefault(n, set()).add(f.parent.name)
            if n not in items or data["fetched_at"] > fetched[n]:
                items[n], fetched[n] = it, data["fetched_at"]
    print(f"read {files} {kind} files: {len(items)} different {kind}")
    return items, teams


# ---------- turn the JSON into rows ----------
prs, pr_teams = read("prs")
pr_rows, pr_team_rows, pr_authored = [], [], []
reviewed, lgtm, approved, touches = {}, {}, {}, set()
for n, p in prs.items():
    pr_rows.append({"num": n, "title": p["title"], "created": p["createdAt"], "merged": p["mergedAt"]})
    pr_team_rows += [{"num": n, "team": t} for t in sorted(pr_teams[n])]
    author = person(p["author"])
    if author:
        pr_authored.append({"login": author, "num": n, "at": p["createdAt"]})
    # The author's own reviews and /approve lines are skipped: they are replies, not reviews.
    for r in p["reviews"]:
        who = person(r["author"])
        if who and who != author and r["submittedAt"]:
            old = reviewed.get((who, n), {"at": r["submittedAt"], "state": r["state"]})
            reviewed[(who, n)] = {
                "at": min(old["at"], r["submittedAt"]),
                "state": max(old["state"], r["state"], key=lambda s: STATE_RANK.get(s, 0)),
            }
    for c in p["comments"]:
        who = person(c["author"])
        if who and who != author:
            if c["lgtm"]:
                keep_earliest(lgtm, (who, n), c["createdAt"])
            if c["approve"]:
                keep_earliest(approved, (who, n), c["createdAt"])
    for path in p["files"]:
        if folder(path):
            touches.add((n, folder(path)))

issues, issue_teams = read("issues")
issue_rows, issue_team_rows, issue_authored = [], [], []
commented, assigned = {}, set()
for n, i in issues.items():
    issue_rows.append({"num": n, "title": i["title"], "created": i["createdAt"],
                       "closed": i["closedAt"], "state": i["state"]})
    issue_team_rows += [{"num": n, "team": t} for t in sorted(issue_teams[n])]
    author = person(i["author"])
    if author:
        issue_authored.append({"login": author, "num": n, "at": i["createdAt"]})
    for c in i["comments"]:
        who = person(c["author"])
        if who and who != author:  # the author already has AUTHORED
            keep_earliest(commented, (who, n), c["createdAt"])
    for a in i["assignees"]:
        who = person(a)
        if who:
            assigned.add((who, n))

# ---------- load into FalkorDB ----------
db = FalkorDB(host=os.environ.get("FALKORDB_HOST") or "localhost",
              port=int(os.environ.get("FALKORDB_PORT") or 6379))
if "k8s" not in db.list_graphs():
    raise SystemExit("No 'k8s' graph yet: run scripts/load_k8s_brain.py first.")
g = db.select_graph("k8s")
for q in ("CREATE INDEX FOR (p:PR) ON (p.num)", "CREATE INDEX FOR (i:Issue) ON (i.num)",
          "CREATE INDEX FOR (f:Folder) ON (f.path)"):
    try:
        g.query(q)
    except Exception as e:
        if "already indexed" not in str(e):
            raise

started = time.time()

# Earlier loaders may have created a Person under a misspelled login (e.g. 'liggit' from
# kep.yaml files). Move that person's edges to the right login, then delete the empty node.
for wrong, right in aliases.items():
    moved = 0
    for rel in ("AUTHORED", "REVIEWED", "APPROVED"):
        moved += g.query(f"""MATCH (a:Person {{login:$wrong}})-[r:{rel}]->(k:KEP)
                             MERGE (b:Person {{login:$right}}) ON CREATE SET b.display=$right
                             MERGE (b)-[:{rel}]->(k) DELETE r""",
                         {"wrong": wrong, "right": right}).relationships_deleted
    moved += g.query("""MATCH (a:Person {login:$wrong})-[r:LEADS]->(t:Team)
                        MERGE (b:Person {login:$right}) ON CREATE SET b.display=$right
                        MERGE (b)-[:LEADS {role:r.role}]->(t) DELETE r""",
                     {"wrong": wrong, "right": right}).relationships_deleted
    g.query("MATCH (a:Person {login:$wrong}) WHERE NOT (a)--() DELETE a", {"wrong": wrong})
    left = g.query("MATCH (a:Person {login:$wrong})-[r]-() RETURN count(r)", {"wrong": wrong}).result_set[0][0]
    if moved or left:
        print(f"merged person {wrong} -> {right}: {int(moved)} edges moved"
              + (f", WARNING: {left} edges of another type still on {wrong}" if left else ""))


def load(query, rows, size=500):
    for i in range(0, len(rows), size):
        g.query(query, {"rows": rows[i:i + size]})


def link(rel, target, edges, props):
    """edges: list of {login, num, <props>}. One MERGEd edge per person per item."""
    sets = " SET " + ", ".join(f"e.{k}=r.{k}" for k in props) if props else ""
    load(f"""UNWIND $rows AS r
             MATCH (p:Person {{login:r.login}}), (x:{target} {{num:r.num}})
             MERGE (p)-[e:{rel}]->(x){sets}""", edges)


load("UNWIND $rows AS r MERGE (p:Person {login:r.login}) ON CREATE SET p.display=r.display",
     [{"login": k, "display": v} for k, v in display.items()])
load("""UNWIND $rows AS r MERGE (x:PR {num:r.num})
        SET x.title=r.title, x.created=r.created, x.merged=r.merged""", pr_rows)
load("""UNWIND $rows AS r MERGE (x:Issue {num:r.num})
        SET x.title=r.title, x.created=r.created, x.closed=r.closed, x.state=r.state""", issue_rows)
load("UNWIND $rows AS r MERGE (:Folder {path:r.path})",
     [{"path": f} for f in sorted({f for _, f in touches})])

link("AUTHORED", "PR", pr_authored, ["at"])
link("REVIEWED", "PR", [{"login": w, "num": n, **v} for (w, n), v in reviewed.items()], ["at", "state"])
link("LGTM", "PR", [{"login": w, "num": n, "at": at} for (w, n), at in lgtm.items()], ["at"])
link("APPROVED", "PR", [{"login": w, "num": n, "at": at} for (w, n), at in approved.items()], ["at"])
link("AUTHORED", "Issue", issue_authored, ["at"])
link("COMMENTED", "Issue", [{"login": w, "num": n, "at": at} for (w, n), at in commented.items()], ["at"])
link("ASSIGNED", "Issue", [{"login": w, "num": n} for w, n in assigned], [])
for target, rows in (("PR", pr_team_rows), ("Issue", issue_team_rows)):
    load(f"""UNWIND $rows AS r MATCH (x:{target} {{num:r.num}}), (t:Team {{key:r.team}})
             MERGE (x)-[:FOR_TEAM]->(t)""", rows)
load("""UNWIND $rows AS r MATCH (x:PR {num:r.num}), (f:Folder {path:r.path})
        MERGE (x)-[:TOUCHES]->(f)""", [{"num": n, "path": f} for n, f in sorted(touches)])
print(f"loaded in {time.time() - started:.0f}s; " + ", ".join(f"{k}: {v}" for k, v in stats.items()))


# ---------- report ----------
def show(title, q, params=None):
    print(f"\n=== {title} ===")
    rows = g.query(q, params or {}).result_set
    for row in rows:
        print("  ", " | ".join(str(x) for x in row))
    return rows


show("Nodes by type", "MATCH (n) RETURN labels(n)[0] AS type, count(*) AS n ORDER BY n DESC")
show("Edges by type (and what they point to)",
     "MATCH ()-[r]->(b) RETURN type(r) AS edge, labels(b)[0] AS to, count(*) AS n ORDER BY n DESC")

for team in TEAMS:
    rows = show(f"{team}: top 8 PR reviewers (REVIEWED + LGTM + APPROVED edges)",
        """MATCH (:Team {key:$team})<-[:FOR_TEAM]-(:PR)<-[e:REVIEWED|LGTM|APPROVED]-(p:Person)
           WITH p, count(e) AS total,
                count(CASE type(e) WHEN 'REVIEWED' THEN 1 END) AS reviewed,
                count(CASE type(e) WHEN 'LGTM' THEN 1 END) AS lgtm,
                count(CASE type(e) WHEN 'APPROVED' THEN 1 END) AS approved
           ORDER BY total DESC, p.login LIMIT 8
           OPTIONAL MATCH (p)-[:LEADS]->(lt:Team)
           WITH p, total, reviewed, lgtm, approved, collect(DISTINCT lt.key) AS leads
           RETURN p.display, total, reviewed, lgtm, approved,
                  CASE WHEN $team IN leads THEN 'LEADER' ELSE 'not a leader' END AS official,
                  [k IN leads WHERE k <> $team] AS leads_other_teams
           ORDER BY total DESC, p.display""", {"team": team})
    leaders = g.query("MATCH (p:Person)-[:LEADS]->(:Team {key:$team}) RETURN count(DISTINCT p)",
                      {"team": team}).result_set[0][0]
    hidden = sum(r[5] != "LEADER" for r in rows)
    print(f"   -> {hidden} of these {len(rows)} are not official leaders ({team} has {leaders} leaders)")
