"""Load real Kubernetes org data (teams + design decisions) into FalkorDB
and ask it 'company brain' questions.

Usage:
  With a FalkorDB server (e.g. Docker on localhost:6379):
      FALKORDB_HOST=localhost python3 load_k8s_brain.py data/
  Without Docker (embedded FalkorDB file, needs: pip install falkordblite):
      python3 load_k8s_brain.py data/ k8s_brain.db

<data_dir> must contain community/sigs.yaml and enhancements/keps/**/kep.yaml
(see the fetch commands in the README).
"""
import glob
import os
import re
import sys

import yaml

data_dir = sys.argv[1]
db_file = sys.argv[2] if len(sys.argv) > 2 else "k8s_brain.db"


def connect():
    """FALKORDB_HOST set -> FalkorDB server. Otherwise -> embedded FalkorDB file."""
    host = os.environ.get("FALKORDB_HOST")
    if host:
        from falkordb import FalkorDB
        return FalkorDB(host=host, port=int(os.environ.get("FALKORDB_PORT", "6379")))
    from redislite.falkordb_client import FalkorDB
    for old in glob.glob(db_file + "*"):
        os.remove(old)
    return FalkorDB(db_file)

# ---------- helpers ----------
def person(x):
    """Turn '@dims', 'dims', 'Derek (@derekwaynecarr)' into a GitHub login."""
    if not isinstance(x, str):
        return None
    m = re.search(r"@([A-Za-z0-9-]+)", x)
    if m:
        return m.group(1)
    s = x.strip().strip("\"',;")
    if re.fullmatch(r"[A-Za-z0-9-]+", s) and s.upper() not in ("TBD", "TODO", "NONE", "NA"):
        return s
    return None

def kep_refs(items):
    """Pull KEP numbers out of '/keps/sig-node/1287-in-place-...' style references."""
    out = []
    for it in items or []:
        s = str(it)
        m = (re.search(r"/(\d+)-", s) or re.search(r"KEP-(\d+)", s, re.I)
             or re.search(r"keps/[^/]+/(\d+)", s))
        if m:
            out.append(str(int(m.group(1))))
    return out

def minor(v):
    m = re.match(r"v?1\.(\d+)", str(v or ""))
    return int(m.group(1)) if m else None

# ---------- read the team registry ----------
org = yaml.safe_load(open(os.path.join(data_dir, "community", "sigs.yaml"), encoding="utf-8"))
teams, leads, subprojects = [], [], []
for kind, group in (("sig", "sigs"), ("workinggroup", "workinggroups"), ("committee", "committees")):
    for t in org.get(group) or []:
        teams.append({"key": t["dir"], "name": t.get("name", t["dir"]), "kind": kind})
        lead = t.get("leadership") or {}
        for role in ("chairs", "tech_leads"):
            for p in lead.get(role) or []:
                login = person(p.get("github", ""))
                if login:
                    leads.append({"login": login.lower(), "display": login, "team": t["dir"], "role": role})
        for sp in t.get("subprojects") or []:
            subprojects.append({"team": t["dir"], "name": sp["name"]})

# ---------- read every design decision (KEP) ----------
keps, people_edges, team_edges, kep_links, gates, stages = [], [], [], [], [], []
for f in glob.glob(os.path.join(data_dir, "enhancements", "keps", "**", "kep.yaml"), recursive=True):
    try:
        k = yaml.safe_load(open(f, encoding="utf-8")) or {}
    except Exception:
        continue
    num = str(k.get("kep-number", "")).strip()
    if not num.isdigit():
        continue
    num = str(int(num))
    keps.append({
        "num": num, "title": str(k.get("title", "")), "status": str(k.get("status", "")),
        "stage": str(k.get("stage", "")), "created": str(k.get("creation-date", "")),
        "latest": str(k.get("latest-milestone", "")), "latest_minor": minor(k.get("latest-milestone")) or -1,
        # Folder inside kubernetes/enhancements, e.g. keps/sig-scheduling/4671-gang-scheduling (for links)
        "path": os.path.relpath(os.path.dirname(f), os.path.join(data_dir, "enhancements")).replace(os.sep, "/"),
    })
    for role, field in (("AUTHORED", "authors"), ("REVIEWED", "reviewers"), ("APPROVED", "approvers")):
        for p in k.get(field) or []:
            login = person(p)
            if login:
                people_edges.append({"login": login.lower(), "display": login, "num": num, "role": role})
    if k.get("owning-sig"):
        team_edges.append({"num": num, "team": str(k["owning-sig"]), "rel": "OWNED_BY"})
    for s in k.get("participating-sigs") or []:
        if str(s) != str(k.get("owning-sig")):
            team_edges.append({"num": num, "team": str(s), "rel": "INVOLVES"})
    for other in kep_refs(k.get("see-also")):
        kep_links.append({"a": num, "b": other, "rel": "SEE_ALSO"})
    for other in kep_refs(k.get("replaces")):
        kep_links.append({"a": num, "b": other, "rel": "REPLACES"})
    for other in kep_refs(k.get("superseded-by")):
        kep_links.append({"a": other, "b": num, "rel": "REPLACES"})
    for g in k.get("feature-gates") or []:
        if isinstance(g, dict) and g.get("name"):
            gates.append({"num": num, "gate": str(g["name"]),
                          "components": [str(c) for c in (g.get("components") or [])]})
    for st, ver in (k.get("milestone") or {}).items():
        if ver:
            stages.append({"num": num, "stage": str(st), "version": str(ver)})

# ---------- load into FalkorDB ----------
db = connect()
g = db.select_graph("k8s")
try:
    g.delete()  # start fresh; harmless if the graph doesn't exist yet
except Exception:
    pass
g = db.select_graph("k8s")
for q in ("CREATE INDEX FOR (k:KEP) ON (k.num)", "CREATE INDEX FOR (p:Person) ON (p.login)",
          "CREATE INDEX FOR (t:Team) ON (t.key)"):
    g.query(q)

def load(query, rows, size=500):
    for i in range(0, len(rows), size):
        g.query(query, {"rows": rows[i:i + size]})

load("UNWIND $rows AS r MERGE (t:Team {key:r.key}) SET t.name=r.name, t.kind=r.kind", teams)
load("""UNWIND $rows AS r MATCH (t:Team {key:r.team})
        MERGE (p:Person {login:r.login}) SET p.display=r.display
        MERGE (p)-[:LEADS {role:r.role}]->(t)""", leads)
load("""UNWIND $rows AS r MATCH (t:Team {key:r.team})
        MERGE (s:Subproject {name:r.name}) MERGE (t)-[:HAS_SUBPROJECT]->(s)""", subprojects)
load("""UNWIND $rows AS r MERGE (k:KEP {num:r.num})
        SET k.title=r.title, k.status=r.status, k.stage=r.stage, k.created=r.created,
            k.latest=r.latest, k.latest_minor=r.latest_minor, k.path=r.path""", keps)
for role in ("AUTHORED", "REVIEWED", "APPROVED"):
    load(f"""UNWIND $rows AS r MATCH (k:KEP {{num:r.num}})
             MERGE (p:Person {{login:r.login}}) ON CREATE SET p.display=r.display
             MERGE (p)-[:{role}]->(k)""", [e for e in people_edges if e["role"] == role])
for rel in ("OWNED_BY", "INVOLVES"):
    load(f"""UNWIND $rows AS r MATCH (k:KEP {{num:r.num}})
             MERGE (t:Team {{key:r.team}}) MERGE (k)-[:{rel}]->(t)""",
         [e for e in team_edges if e["rel"] == rel])
for rel in ("SEE_ALSO", "REPLACES"):
    load(f"""UNWIND $rows AS r MATCH (a:KEP {{num:r.a}}), (b:KEP {{num:r.b}})
             WHERE a <> b MERGE (a)-[:{rel}]->(b)""", [e for e in kep_links if e["rel"] == rel])
load("""UNWIND $rows AS r MATCH (k:KEP {num:r.num})
        MERGE (f:FeatureGate {name:r.gate}) MERGE (k)-[:INTRODUCES]->(f)
        WITH f, r UNWIND r.components AS c
        MERGE (comp:Component {name:c}) MERGE (f)-[:USED_IN]->(comp)""", gates)
load("""UNWIND $rows AS r MATCH (k:KEP {num:r.num})
        MERGE (v:Release {version:r.version}) MERGE (k)-[:REACHED {stage:r.stage}]->(v)""", stages)

# ---------- ask it questions ----------
def show(title, q, params=None):
    print(f"\n=== {title} ===")
    rows = g.query(q, params or {}).result_set
    for row in rows:
        print("  ", " | ".join(str(x) for x in row))
    return rows

show("What's in the brain",
     """MATCH (n) RETURN labels(n)[0] AS type, count(*) AS how_many ORDER BY how_many DESC""")
show("Connections",
     """MATCH ()-[r]->() RETURN type(r) AS edge, count(*) AS how_many ORDER BY how_many DESC""")

owners = show("OWNS: official leaders of SIG Node (from the team registry)",
     """MATCH (p:Person)-[l:LEADS]->(:Team {key:'sig-node'})
        RETURN p.display, collect(l.role) ORDER BY p.display""")
knowers = show("KNOWS: people most involved in SIG Node's design decisions (authored/reviewed/approved)",
     """MATCH (p:Person)-[r:AUTHORED|REVIEWED|APPROVED]->(k:KEP)-[:OWNED_BY]->(:Team {key:'sig-node'})
        RETURN p.display, count(DISTINCT k) AS decisions
        ORDER BY decisions DESC LIMIT 8""")
official = {r[0].lower() for r in owners}
hidden = [r[0] for r in knowers if r[0].lower() not in official]
print(f"   -> In the top 8 'knowers', {len(hidden)} are NOT official leaders: {hidden}")

chain = show("DECISION TRAIL: the longest chain of decisions replacing older decisions",
     """MATCH p = (a:KEP)-[:REPLACES*1..6]->(b:KEP)
        WHERE NOT ()-[:REPLACES]->(a)
        RETURN [n IN nodes(p) | 'KEP-' + n.num + ' (' + n.status + ')'] AS chain, length(p) AS hops
        ORDER BY hops DESC LIMIT 1""")

show("HOW THE COMPANY REALLY WORKS: team pairs that share the most decisions",
     """MATCH (a:Team)<-[:OWNED_BY|INVOLVES]-(k:KEP)-[:OWNED_BY|INVOLVES]->(b:Team)
        WHERE a.key < b.key
        RETURN a.key, b.key, count(DISTINCT k) AS shared_decisions
        ORDER BY shared_decisions DESC LIMIT 6""")

latest = g.query("MATCH (k:KEP) RETURN max(k.latest_minor)").result_set[0][0]
show(f"CURRENCY CHECK: decisions still 'implementable' at alpha, untouched for 8+ releases (newest release in data: v1.{latest})",
     """MATCH (k:KEP)-[:OWNED_BY]->(t:Team)
        WHERE k.status = 'implementable' AND k.stage = 'alpha'
          AND k.latest_minor >= 0 AND k.latest_minor <= $cutoff
        RETURN count(k) AS stale_decisions, collect(t.key)[0..5] AS sample_teams""",
     {"cutoff": latest - 8})

gate = g.query("""MATCH (k:KEP)-[:INTRODUCES]->(f:FeatureGate)
                  MATCH (k)-[:INVOLVES]->(t:Team)
                  RETURN f.name, count(DISTINCT t) AS teams ORDER BY teams DESC LIMIT 1""").result_set[0][0]
show(f"WHO DO I ASK about the feature switch '{gate}'? (owner team, decision, deciders)",
     """MATCH (f:FeatureGate {name:$gate})<-[:INTRODUCES]-(k:KEP)-[:OWNED_BY]->(t:Team)
        OPTIONAL MATCH (ap:Person)-[:APPROVED]->(k)
        OPTIONAL MATCH (k)-[:INVOLVES]->(o:Team)
        RETURN 'KEP-' + k.num, k.title, k.stage, t.key AS owning_team,
               collect(DISTINCT ap.display) AS approved_by, collect(DISTINCT o.key) AS also_involves""",
     {"gate": gate})
