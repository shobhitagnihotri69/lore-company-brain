"""Link every Folder in the graph to its official owners from the OWNERS files.

Run after load_github.py (it makes the Folder nodes). Only adds (MERGE), never deletes,
so running it twice is safe:

    FALKORDB_HOST=localhost python scripts/load_owners.py

Adds:
  (Person)-[:APPROVER_OF {source}]->(Folder)
  (Person)-[:REVIEWER_OF {source}]->(Folder)
`source` is the OWNERS file that lists the person: e.g. 'pkg/scheduler/OWNERS', or 'OWNERS'
for the repo root (root approvers can approve almost everything, so filter on it when needed).
"""
import os

from falkordb import FalkorDB

import owners
from owners import owners_for_path

EXAMPLES = ["pkg/scheduler/framework", "staging/src/k8s.io/kubelet/pkg"]

db = FalkorDB(host=os.environ.get("FALKORDB_HOST") or "localhost",
              port=int(os.environ.get("FALKORDB_PORT") or 6379))
if "k8s" not in db.list_graphs():
    raise SystemExit("No 'k8s' graph yet: run scripts/load_k8s_brain.py first.")
g = db.select_graph("k8s")
folders = [r[0] for r in g.query("MATCH (f:Folder) RETURN f.path").result_set]
if not folders:
    raise SystemExit("No Folder nodes yet: run scripts/load_github.py first.")


def load(query, rows, size=500):
    """Run `query` on rows in batches of 500. Returns (nodes created, edges created)."""
    made = [0, 0]
    for i in range(0, len(rows), size):
        res = g.query(query, {"rows": rows[i:i + size]})
        made[0] += res.nodes_created
        made[1] += res.relationships_created
    return int(made[0]), int(made[1])


rows = {"APPROVER_OF": [], "REVIEWER_OF": []}
for path in folders:
    found = owners_for_path(path)
    for rel, role in (("APPROVER_OF", "approvers"), ("REVIEWER_OF", "reviewers")):
        rows[rel] += [{"login": login, "path": path, "source": src} for login, src in found[role].items()]

people = sorted({r["login"] for rs in rows.values() for r in rs})
new_people, _ = load("UNWIND $rows AS r MERGE (p:Person {login:r.login}) ON CREATE SET p.display=r.login",
                     [{"login": p} for p in people])
print(f"{len(owners.owners_files())} OWNERS files, {len(folders)} folders in the graph, "
      f"{len(people)} official owners ({new_people} new Person nodes)")
for rel, rs in rows.items():
    _, new_edges = load(f"""UNWIND $rows AS r
                            MATCH (p:Person {{login:r.login}}), (f:Folder {{path:r.path}})
                            MERGE (p)-[e:{rel}]->(f) SET e.source=r.source""", rs)
    total = g.query(f"MATCH (:Person)-[e:{rel}]->(:Folder) RETURN count(e)").result_set[0][0]
    from_root = sum(r["source"] == "OWNERS" for r in rs)
    print(f"{rel}: {total} edges in the graph ({new_edges} new this run; "
          f"{from_root} come from the root OWNERS file)")
if owners.unknown_teams:
    print("skipped team names that OWNERS_ALIASES doesn't define:", ", ".join(sorted(owners.unknown_teams)))

for path in EXAMPLES:
    print(f"\n=== {path} ===")
    in_graph = g.query("MATCH (f:Folder {path:$p}) RETURN count(f)", {"p": path}).result_set[0][0]
    if not in_graph:
        print("   (not a Folder in the graph yet; straight from the OWNERS files)")
    for rel, role in (("APPROVER_OF", "approvers"), ("REVIEWER_OF", "reviewers")):
        if in_graph:
            groups = g.query(f"""MATCH (p:Person)-[e:{rel}]->(:Folder {{path:$p}})
                                 WITH e.source AS source, p.login AS login ORDER BY login
                                 RETURN source, collect(login) ORDER BY size(source) DESC""",
                             {"p": path}).result_set
        else:
            found = owners_for_path(path)[role]
            groups = sorted(((s, sorted(l for l, src in found.items() if src == s)) for s in set(found.values())),
                            key=lambda x: -len(x[0]))
        for source, logins in groups:
            print(f"   {role:9} from {source}: {', '.join(logins)}")
