"""Lore's graph tools: real Cypher over the FalkorDB graph `k8s`.

Every tool returns the same envelope:
    {"tool": name,
     "summary": a short plain-English answer,
     "data": the tool's fields (described on each function),
     "evidence": {"nodes": [...], "edges": [...]},   # a small map, at most 25 nodes
     "sources": [{"type", "ref", "title", "url"}],   # real links
     "is_stub": False}

    who_owns(thing)               official owners of a team, folder or KEP
    who_knows(thing, as_of=None)  who really does the work, from KEP and PR evidence
    why(thing)                    the decision trail of a KEP or feature gate
    whats_stale(team=None)        KEPs stuck at implementable + alpha for 8+ releases
    route_issue(title, body)      team, related KEPs, people and a draft comment for an issue

`thing` can be a team ('sig-node', 'SIG Node', 'node'), a KEP ('KEP-4671', '4671'), a folder or
file path ('pkg/scheduler/framework/...'), a feature gate name, or a typed id ('team:sig-node').

Evidence nodes are {id, label, type, ...} and edges {source, target, relation, ...}, the same
field names as the `graph` part of /api/ask. Node ids are '<type>:<key>': team:sig-node,
person:liggitt, kep:4671, pr:12345, folder:pkg/scheduler/framework, gate:GenericWorkload.
Every edge's source and target are in `nodes`.

USE_STUBS=1 returns the old fixed stub answers instead (is_stub: True), e.g. for a demo
without a loaded graph. A FalkorDB connection error is raised, not hidden.
"""
import math
import os
import re
from datetime import date, timedelta
from functools import lru_cache

MAX_NODES = 25
RECENT_DAYS = 90
TOP_PEOPLE = 8
ROLE = {"chairs": "chair", "tech_leads": "tech_lead"}
KINDS = ["kep_authored", "kep_reviewed", "kep_approved",
         "pr_authored", "pr_reviewed", "pr_lgtm", "pr_approved"]

TEAM_PAGE = "https://github.com/kubernetes/community/tree/master/{}"
SIGS_YAML = "https://github.com/kubernetes/community/blob/master/sigs.yaml"
KEP_PAGE = "https://github.com/kubernetes/enhancements/tree/master/{}"
KEP_ISSUE = "https://github.com/kubernetes/enhancements/issues/{}"
PR_PAGE = "https://github.com/kubernetes/kubernetes/pull/{}"
OWNERS_PAGE = "https://github.com/kubernetes/kubernetes/blob/master/{}"

STOPWORDS = set("""
a about after again all also am an and any are as at be because been before being but by can
cannot could did do does doing done during each even for from get gets got had has have having
how however i if in into is it its just like make makes may me more most my need needs new no
not now of off on once one only or other our out over please same see should so some still such
than that the their them then there these they this those through to too under until up us use
used uses using very was we were what when where which while who why will with would yet you
your bug issue issues problem error errors fails failed failing works working expected actual
rest stay stays partly part keep kept seem seems look looks wrong right never always every nothing
something anything someone time times way thing things lot
""".split())


# ── graph access ────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def _graph():
    from falkordb import FalkorDB
    db = FalkorDB(host=os.getenv("FALKORDB_HOST") or "localhost",
                  port=int(os.getenv("FALKORDB_PORT") or "6379"))
    return db.select_graph("k8s")


def _q(query, **params):
    return _graph().query(query, params).result_set


# ── evidence map ────────────────────────────────────────────────────────
class Evidence:
    """Nodes and edges for the evidence map. Stops adding nodes at MAX_NODES;
    build() drops any edge whose source or target was not kept, so no edge dangles."""

    def __init__(self):
        self.nodes, self.edges = {}, []

    def node(self, node_id, label, node_type, **extra):
        if node_id not in self.nodes and len(self.nodes) < MAX_NODES:
            self.nodes[node_id] = {"id": node_id, "label": label, "type": node_type, **extra}
        return node_id in self.nodes

    def edge(self, source, target, relation, **extra):
        self.edges.append({"source": source, "target": target, "relation": relation, **extra})

    def build(self):
        seen, edges = set(), []
        for e in self.edges:
            key = (e["source"], e["target"], e["relation"])
            if e["source"] in self.nodes and e["target"] in self.nodes and key not in seen:
                seen.add(key)
                edges.append(e)
        return {"nodes": list(self.nodes.values()), "edges": edges}


def _team_id(key): return f"team:{key}"
def _person_id(login): return f"person:{login}"
def _kep_id(num): return f"kep:{num}"
def _pr_id(num): return f"pr:{num}"
def _folder_id(path): return f"folder:{path}"
def _gate_id(name): return f"gate:{name}"


def _envelope(tool, summary, data, ev, sources):
    unique = list({(s["type"], s["ref"]): s for s in sources}.values())
    return {"tool": tool, "summary": summary, "data": data, "evidence": ev.build(),
            "sources": unique, "is_stub": False}


def _not_found(tool, thing, what="a team, folder, KEP or feature gate"):
    return _envelope(tool, f"Lore found no {what} matching '{thing}'.",
                     {"thing": None, "query": thing}, Evidence(), [])


# ── looking things up ───────────────────────────────────────────────────
def _team_info(key):
    rows = _q("MATCH (t:Team {key:$k}) RETURN t.key, t.name, t.kind", k=key)
    if not rows:
        return None
    key, name, kind = rows[0]
    # Only real teams from sigs.yaml (kind set) have a page; typo teams from KEP files don't.
    return {"type": "team", "key": key, "name": name, "kind": kind,
            "url": TEAM_PAGE.format(key) if kind else None}


def _team_source(team):
    return {"type": "TEAM", "ref": team["key"], "title": f"{team['key']} page in kubernetes/community",
            "url": team["url"] or SIGS_YAML}


def _kep_url(num, path):
    return KEP_PAGE.format(path) if path else KEP_ISSUE.format(num)


KEP_FIELDS = "k.num, k.title, k.status, k.stage, k.created, k.latest, k.path"


def _kep_row(row, team=None):
    num, title, status, stage, created, latest, path = row
    return {"type": "kep", "num": num, "ref": f"KEP-{num}", "title": (title or "").strip(),
            "status": status, "stage": stage, "created": created, "latest": latest,
            "team": team, "url": _kep_url(num, path)}


def _kep_info(num):
    rows = _q(f"MATCH (k:KEP {{num:$n}}) OPTIONAL MATCH (k)-[:OWNED_BY]->(t:Team) "
              f"RETURN {KEP_FIELDS}, t.key LIMIT 1", n=num)
    return _kep_row(rows[0][:7], rows[0][7]) if rows else None


def _kep_source(kep):
    return {"type": "KEP", "ref": kep["ref"], "title": kep["title"], "url": kep["url"]}


def _find_team(text):
    t = text.lower().strip()
    dashed = re.sub(r"\s+", "-", t)
    keys = {dashed, "sig-" + dashed, "wg-" + dashed}
    rows = _q("MATCH (t:Team) RETURN t.key, t.name, t.kind")
    for key, _, _ in rows:
        if key == dashed:
            return key
    for key, name, kind in rows:
        name = (name or "").lower()
        if kind and (key in keys or t in (name, f"sig {name}", f"wg {name}")):
            return key
    return None


def _find_folder(path):
    """The Folder node for a folder or file path: the longest Folder that contains it."""
    p = path.replace("\\", "/").strip("/")
    rows = _q("""MATCH (f:Folder) WHERE f.path = $p OR $p STARTS WITH (f.path + '/')
                 RETURN f.path ORDER BY size(f.path) DESC LIMIT 1""", p=p)
    return rows[0][0] if rows else None


def _resolve(thing):
    """-> (kind, key) with kind in team / kep / folder / gate, or (None, None)."""
    text = str(thing or "").strip()
    if not text:
        return None, None
    prefix, _, rest = text.partition(":")
    if rest and prefix.lower() in ("team", "kep", "folder", "gate"):
        kind, text = prefix.lower(), rest.strip()
    else:
        kind = None
    m = re.fullmatch(r"(?:kep[-\s#]*)?(\d+)", text, re.I)
    if kind in (None, "kep") and m:
        num = str(int(m.group(1)))
        if _q("MATCH (k:KEP {num:$n}) RETURN count(k)", n=num)[0][0]:
            return "kep", num
    if kind in (None, "team"):
        key = _find_team(text)
        if key:
            return "team", key
    if kind == "folder" or (kind is None and ("/" in text or "\\" in text)):
        path = _find_folder(text)
        if path:
            return "folder", path
    if kind in (None, "gate"):
        rows = _q("MATCH (f:FeatureGate) WHERE toLower(f.name) = toLower($n) RETURN f.name LIMIT 1", n=text)
        if rows:
            return "gate", rows[0][0]
    if kind in (None, "kep"):
        rows = _q("MATCH (k:KEP) WHERE toLower(trim(k.title)) = toLower($t) RETURN k.num LIMIT 1", t=text)
        if rows:
            return "kep", rows[0][0]
    return None, None


def _gate_kep(name):
    """The newest KEP that introduces a feature gate."""
    rows = _q("""MATCH (k:KEP)-[:INTRODUCES]->(:FeatureGate {name:$n})
                 RETURN k.num ORDER BY k.created DESC LIMIT 1""", n=name)
    return rows[0][0] if rows else None


def _leads(team_key):
    rows = _q("""MATCH (p:Person)-[l:LEADS]->(:Team {key:$k})
                 RETURN p.login, p.display, collect(DISTINCT l.role) ORDER BY p.login""", k=team_key)
    return [{"login": login, "display": display, "team": team_key,
             "roles": sorted({ROLE.get(r, r) for r in roles}),
             "role": ", ".join(sorted({ROLE.get(r, r) for r in roles}))}
            for login, display, roles in rows]


# ── who_owns ────────────────────────────────────────────────────────────
def who_owns(thing):
    """Official owners.
    Team   -> its leaders from sigs.yaml (LEADS, role chair / tech_lead).
    Folder -> approvers and reviewers from OWNERS files (APPROVER_OF / REVIEWER_OF, with the file).
    KEP    -> its owning team's leaders plus the KEP's approvers. A feature gate uses its KEP.
    data: {thing, owners} for a team; {thing, approvers, reviewers} for a folder;
          {thing, team, team_leads, approvers} for a KEP."""
    if _use_stubs():
        return _stub("who_owns")
    kind, key = _resolve(thing)
    if kind == "gate":
        kind, key = "kep", _gate_kep(key)
    ev = Evidence()

    if kind == "team":
        team, leads = _team_info(key), _leads(key)
        ev.node(_team_id(key), key, "Team")
        for p in leads:
            ev.node(_person_id(p["login"]), p["display"], "Person", status="owner")
            ev.edge(_person_id(p["login"]), _team_id(key), "LEADS", role=p["role"])
        summary = (f"{key} has {len(leads)} official leaders in sigs.yaml: "
                   + ", ".join(f"{p['display']} ({p['role']})" for p in leads) + ".")
        sources = [_team_source(team), {"type": "REGISTRY", "ref": "sigs.yaml",
                                        "title": "Kubernetes team registry", "url": SIGS_YAML}]
        return _envelope("who_owns", summary, {"thing": team, "owners": leads}, ev, sources)

    if kind == "folder":
        rows = _q("""MATCH (p:Person)-[e:APPROVER_OF|REVIEWER_OF]->(:Folder {path:$p})
                     RETURN p.login, p.display, type(e), e.source""", p=key)
        nearest_first = sorted(rows, key=lambda r: (-len(r[3]), r[0]))
        approvers = [{"login": l, "display": d, "source": s} for l, d, t, s in nearest_first if t == "APPROVER_OF"]
        reviewers = [{"login": l, "display": d, "source": s} for l, d, t, s in nearest_first if t == "REVIEWER_OF"]
        ev.node(_folder_id(key), key, "Folder")
        for rel, people in (("APPROVER_OF", approvers), ("REVIEWER_OF", reviewers)):
            for p in people:
                if ev.node(_person_id(p["login"]), p["display"], "Person", status="owner"):
                    # The graph calls the OWNERS file `source`; in the map `source` is the start node.
                    ev.edge(_person_id(p["login"]), _folder_id(key), rel, owners_file=p["source"])
        files = list(dict.fromkeys(p["source"] for p in approvers + reviewers))
        by_file = {f: [p["display"] for p in approvers if p["source"] == f] for f in files}
        summary = (f"{key} has {len(approvers)} official approvers and {len(reviewers)} reviewers "
                   f"in OWNERS files. " + " ".join(f"Approvers from {f}: {', '.join(names)}."
                                                  for f, names in by_file.items() if names))
        sources = [{"type": "OWNERS", "ref": f, "title": "OWNERS file in kubernetes/kubernetes",
                    "url": OWNERS_PAGE.format(f)} for f in files]
        data = {"thing": {"type": "folder", "path": key}, "approvers": approvers, "reviewers": reviewers}
        return _envelope("who_owns", summary, data, ev, sources)

    if kind == "kep" and key:
        kep = _kep_info(key)
        approvers = [{"login": l, "display": d} for l, d in _q(
            "MATCH (p:Person)-[:APPROVED]->(:KEP {num:$n}) RETURN p.login, p.display ORDER BY p.login", n=key)]
        leads = _leads(kep["team"]) if kep["team"] else []
        ev.node(_kep_id(key), kep["ref"], "KEP", title=kep["title"])
        if kep["team"]:
            ev.node(_team_id(kep["team"]), kep["team"], "Team")
            ev.edge(_kep_id(key), _team_id(kep["team"]), "OWNED_BY")
        for p in approvers:
            ev.node(_person_id(p["login"]), p["display"], "Person", status="owner")
            ev.edge(_person_id(p["login"]), _kep_id(key), "APPROVED")
        for p in leads:
            ev.node(_person_id(p["login"]), p["display"], "Person", status="owner")
            ev.edge(_person_id(p["login"]), _team_id(kep["team"]), "LEADS", role=p["role"])
        summary = (f"{kep['ref']} '{kep['title']}' is owned by {kep['team'] or 'no team'}. "
                   f"Approvers: {', '.join(p['display'] for p in approvers) or 'none listed'}. "
                   f"{kep['team']} leaders: {', '.join(p['display'] for p in leads) or 'none'}.")
        sources = [_kep_source(kep)]
        if kep["team"]:
            sources.append(_team_source(_team_info(kep["team"])))
        data = {"thing": kep, "team": kep["team"], "team_leads": leads, "approvers": approvers}
        return _envelope("who_owns", summary, data, ev, sources)

    return _not_found("who_owns", thing, "team, folder or KEP")


# ── who_knows ───────────────────────────────────────────────────────────
KEP_WORK = """
MATCH (:Team {key:$key})<-[:OWNED_BY]-(k:KEP)<-[e:AUTHORED|REVIEWED|APPROVED]-(p:Person)
WHERE $no_cut OR (k.created >= '2000' AND k.created < $cut)
RETURN p.login, p.display, 'kep_' + toLower(type(e)), count(DISTINCT k),
       count(CASE WHEN k.created >= $recent_from AND k.created < '3000' THEN 1 END)"""
PR_WORK = """
MATCH ({target})<-[:{via}]-(pr:PR)<-[e:AUTHORED|REVIEWED|LGTM|APPROVED]-(p:Person)
WHERE e.at < $cut
RETURN p.login, p.display, 'pr_' + toLower(type(e)), count(DISTINCT pr),
       count(CASE WHEN e.at >= $recent_from THEN 1 END)"""
KEP_ITEMS = """
MATCH (:Team {key:$key})<-[:OWNED_BY]-(k:KEP)<-[e:AUTHORED|REVIEWED|APPROVED]-(p:Person)
WHERE p.login IN $logins AND ($no_cut OR (k.created >= '2000' AND k.created < $cut))
WITH p, k, e ORDER BY k.created DESC
RETURN p.login, collect([k.num, k.title, type(e), k.created])[0..2]"""
PR_ITEMS = """
MATCH ({target})<-[:{via}]-(pr:PR)<-[e:AUTHORED|REVIEWED|LGTM|APPROVED]-(p:Person)
WHERE p.login IN $logins AND e.at < $cut
WITH p, pr, e ORDER BY e.at DESC
RETURN p.login, collect([pr.num, pr.title, type(e), e.at])[0..2]"""


def _window(as_of):
    """-> (cut, no_cut, recent_from, as_of_day). Raises ValueError for a bad date."""
    if as_of:
        day = date.fromisoformat(str(as_of)[:10])
        return day.isoformat(), False, (day - timedelta(days=RECENT_DAYS)).isoformat(), day.isoformat()
    today = date.today()
    return "9999-12-31", True, (today - timedelta(days=RECENT_DAYS)).isoformat(), None


def _work(kind, key, cut, no_cut, recent_from):
    """Per-person evidence counts. A team counts KEPs it owns and PRs labelled for it;
    a folder counts PRs that touch it. Each kind counts distinct KEPs or PRs."""
    people = {}
    queries = []
    if kind == "team":
        queries = [KEP_WORK, PR_WORK.format(target=":Team {key:$key}", via="FOR_TEAM")]
    elif kind == "folder":
        queries = [PR_WORK.format(target=":Folder {path:$key}", via="TOUCHES")]
    for query in queries:
        for login, display, what, n, recent in _q(query, key=key, cut=cut, no_cut=no_cut,
                                                   recent_from=recent_from):
            p = people.setdefault(login, {"login": login, "display": display, "total": 0, "recent": 0,
                                          "counts": dict.fromkeys(KINDS, 0)})
            p["counts"][what] += int(n)
            p["total"] += int(n)
            p["recent"] += int(recent)
    return people


def _official(kind, key, kep=None):
    """{login: [roles]} of official owners for a team, folder or KEP."""
    out = {}
    if kind == "team":
        for p in _leads(key):
            out[p["login"]] = p["roles"]
    elif kind == "folder":
        for login, rel in _q("""MATCH (p:Person)-[e:APPROVER_OF|REVIEWER_OF]->(:Folder {path:$p})
                                RETURN p.login, type(e)""", p=key):
            out.setdefault(login, []).append("approver" if rel == "APPROVER_OF" else "reviewer")
    elif kind == "kep":
        if kep and kep["team"]:
            out.update(_official("team", kep["team"]))
        for (login,) in _q("MATCH (p:Person)-[:APPROVED]->(:KEP {num:$n}) RETURN p.login", n=key):
            out.setdefault(login, []).append("kep_approver")
    return out


def _ranked(people):
    return sorted(people.values(), key=lambda p: (-p["total"], -p["recent"], p["login"]))


def _knowers(kind, key, as_of=None):
    """Shared by who_knows and route_issue. Returns the ranked people and context."""
    cut, no_cut, recent_from, as_of_day = _window(as_of)
    kep = _kep_info(key) if kind == "kep" else None
    if kind == "kep":
        # People who worked on this KEP, ranked by all their work for the KEP's owning team.
        on_kep = {}
        for login, display, rel in _q("""MATCH (p:Person)-[e:AUTHORED|REVIEWED|APPROVED]->(:KEP {num:$n})
                                         RETURN p.login, p.display, type(e)""", n=key):
            on_kep.setdefault(login, {"display": display, "roles": []})["roles"].append(rel.lower())
        work = _work("team", kep["team"], cut, no_cut, recent_from) if kep["team"] else {}
        people = {}
        for login, info in on_kep.items():
            p = work.get(login) or {"login": login, "display": info["display"], "total": 0, "recent": 0,
                                    "counts": dict.fromkeys(KINDS, 0)}
            people[login] = {**p, "on_this_kep": sorted(info["roles"])}
    else:
        people = _work(kind, key, cut, no_cut, recent_from)
    official = _official(kind, key, kep)
    ranked = _ranked(people)
    for p in ranked:
        p["is_official_owner"] = p["login"] in official
        p["official_roles"] = official.get(p["login"], [])
    return {"ranked": ranked, "official": official, "kep": kep, "cut": cut, "no_cut": no_cut,
            "recent_from": recent_from, "as_of": as_of_day}


def _recent_items(kind, key, logins, cut, no_cut):
    """The 2 most recent pieces of evidence per person: [(node_id, label, type, rel, date, link)]."""
    items = {login: [] for login in logins}
    if not logins:
        return items
    if kind == "team":
        for login, rows in _q(KEP_ITEMS, key=key, logins=logins, cut=cut, no_cut=no_cut):
            items[login] += [(_kep_id(n), f"KEP-{n}", "KEP", rel, d, "OWNED_BY") for n, _t, rel, d in rows]
        target, via, link = ":Team {key:$key}", "FOR_TEAM", "FOR_TEAM"
    else:
        target, via, link = ":Folder {path:$key}", "TOUCHES", "TOUCHES"
    for login, rows in _q(PR_ITEMS.format(target=target, via=via), key=key, logins=logins, cut=cut):
        items[login] += [(_pr_id(n), f"PR #{n}", "PR", rel, at, link) for n, _t, rel, at in rows]
    for login in items:
        items[login] = sorted(items[login], key=lambda i: i[4] or "", reverse=True)[:2]
    return items


def _person_label(p):
    return p.get("display") or p["login"]


def who_knows(thing, as_of=None):
    """Who really does the work on a team, folder or KEP (a feature gate uses its KEP).
    Counts distinct evidence: KEPs authored / reviewed / approved (teams and KEPs) and PRs
    authored / reviewed / lgtm / approved (teams via FOR_TEAM, folders via TOUCHES).
    as_of='YYYY-MM-DD' keeps only evidence dated before that day. `recent` counts the
    90 days before as_of (or before today). For a KEP: the people who worked on it, ranked
    by their work for its owning team.
    data: {thing, as_of, recent_window, people: [{login, display, total, recent, counts,
           is_official_owner, official_roles}], official_owners, not_official_in_top_8}"""
    if _use_stubs():
        return _stub("who_knows")
    kind, key = _resolve(thing)
    if kind == "gate":
        kind, key = "kep", _gate_kep(key)
    if kind not in ("team", "folder", "kep") or not key:
        return _not_found("who_knows", thing, "team, folder or KEP")
    try:
        k = _knowers(kind, key, as_of)
    except ValueError:
        return _envelope("who_knows", f"as_of must be a date like 2026-07-01, not '{as_of}'.",
                         {"thing": None, "query": thing, "as_of": as_of}, Evidence(), [])
    ranked, official = k["ranked"], k["official"]
    top = ranked[:TOP_PEOPLE]

    ev = Evidence()
    if kind == "team":
        thing_info, thing_id = _team_info(key), _team_id(key)
        ev.node(thing_id, key, "Team")
        sources = [_team_source(thing_info)]
    elif kind == "folder":
        thing_info, thing_id = {"type": "folder", "path": key}, _folder_id(key)
        ev.node(thing_id, key, "Folder")
        sources = []
    else:
        thing_info, thing_id = k["kep"], _kep_id(key)
        ev.node(thing_id, thing_info["ref"], "KEP", title=thing_info["title"])
        sources = [_kep_source(thing_info)]
        if thing_info["team"]:
            ev.node(_team_id(thing_info["team"]), thing_info["team"], "Team")
            ev.edge(thing_id, _team_id(thing_info["team"]), "OWNED_BY")
    for p in top:
        ev.node(_person_id(p["login"]), _person_label(p), "Person",
                status="owner_and_knower" if p["is_official_owner"] else "knower",
                weight=p["total"])
        if kind == "team" and p["is_official_owner"]:
            ev.edge(_person_id(p["login"]), thing_id, "LEADS", role=", ".join(p["official_roles"]))
        if kind == "folder" and "approver" in p["official_roles"]:
            ev.edge(_person_id(p["login"]), thing_id, "APPROVER_OF")
        for rel in p.get("on_this_kep", []):
            ev.edge(_person_id(p["login"]), thing_id, rel.upper())
    if kind in ("team", "folder"):
        items = _recent_items(kind, key, [p["login"] for p in top], k["cut"], k["no_cut"])
        for p in top:
            for node_id, label, ntype, rel, when, link in items[p["login"]]:
                if ev.node(node_id, label, ntype):
                    ev.edge(_person_id(p["login"]), node_id, rel, at=when)
                    ev.edge(node_id, thing_id, link)
                    if ntype == "PR":
                        sources.append({"type": "PR", "ref": label, "title": "kubernetes/kubernetes pull request",
                                        "url": PR_PAGE.format(node_id.split(":", 1)[1])})

    rank = {p["login"]: i + 1 for i, p in enumerate(ranked)}
    by_login = {p["login"]: p for p in ranked}
    officials = [{"login": login, "roles": roles, "rank": rank.get(login),
                  "total": by_login.get(login, {}).get("total", 0),
                  "recent": by_login.get(login, {}).get("recent", 0)} for login, roles in sorted(official.items())]
    hidden = sum(not p["is_official_owner"] for p in top)
    name = thing_info.get("ref") or thing_info.get("key") or thing_info.get("path")
    when = f" before {k['as_of']}" if k["as_of"] else ""
    if top:
        summary = (f"Who really does the work on {name}{when}: "
                   + "; ".join(f"{_person_label(p)} ({p['total']} pieces of evidence, {p['recent']} recent"
                               + (", official owner" if p["is_official_owner"] else "") + ")" for p in top[:5])
                   + f". {hidden} of the top {len(top)} are not official owners.")
    else:
        summary = f"Lore has no recorded work on {name}{when}."
    data = {"thing": thing_info, "as_of": k["as_of"],
            "recent_window": {"from": k["recent_from"], "to": k["as_of"] or date.today().isoformat()},
            "people": ranked[:10], "official_owners": officials, "not_official_in_top_8": hidden}
    return _envelope("who_knows", summary, data, ev, sources)


# ── why ─────────────────────────────────────────────────────────────────
def why(thing):
    """The decision trail of a KEP or feature gate: what it replaces and what replaced it
    (REPLACES, both ways), related KEPs (SEE_ALSO), status, stage, dates, release milestones,
    approvers, authors, feature gates and owning team. Free text falls back to the KEP whose
    title matches it best.
    data: {kep, feature_gate, trail (oldest first), replaces, replaced_by, see_also,
           milestones, approvers, authors, feature_gates, matched_by}"""
    if _use_stubs():
        return _stub("why")
    kind, key = _resolve(thing)
    gate, matched_by = None, "id"
    if kind == "gate":
        gate, (kind, key) = key, ("kep", _gate_kep(key))
    if kind != "kep" or not key:
        hits = _kep_matches(_words(str(thing)), limit=1)
        if not hits:
            return _not_found("why", thing, "KEP or feature gate")
        kind, key, matched_by = "kep", hits[0]["num"], "title words"
    kep = _kep_info(key)

    newer, older = _q("""MATCH (k:KEP {num:$n})
                         OPTIONAL MATCH (a:KEP)-[:REPLACES*1..6]->(k)
                         WITH k, collect(DISTINCT a.num) AS newer
                         OPTIONAL MATCH (k)-[:REPLACES*1..6]->(b:KEP)
                         RETURN newer, collect(DISTINCT b.num)""", n=key)[0]
    chain_nums = list(dict.fromkeys(newer + [key] + older))
    chain = {r[0]: _kep_row(r[:7], r[7]) for r in _q(
        f"""MATCH (k:KEP) WHERE k.num IN $nums OPTIONAL MATCH (k)-[:OWNED_BY]->(t:Team)
            RETURN {KEP_FIELDS}, t.key""", nums=chain_nums)}
    trail = sorted(chain.values(), key=lambda c: c["created"] or "")
    links = _q("""MATCH (a:KEP)-[:REPLACES]->(b:KEP) WHERE a.num IN $nums AND b.num IN $nums
                  RETURN a.num, b.num""", nums=chain_nums)
    replaces = [chain[b] for a, b in links if a == key]
    replaced_by = [chain[a] for a, b in links if b == key]
    see_also = []
    for row in _q(f"""MATCH (m:KEP {{num:$n}})-[r:SEE_ALSO]-(k:KEP) WHERE NOT k.num IN $nums
                      OPTIONAL MATCH (k)-[:OWNED_BY]->(t:Team)
                      RETURN DISTINCT {KEP_FIELDS}, t.key, startNode(r).num = m.num""", n=key, nums=chain_nums):
        see_also.append({**_kep_row(row[:7], row[7]), "points_from_this_kep": row[8]})
    see_also = list({s["num"]: s for s in see_also}.values())
    people = _q("""MATCH (p:Person)-[r:APPROVED|AUTHORED]->(:KEP {num:$n})
                   RETURN type(r), p.login, p.display ORDER BY p.login""", n=key)
    approvers = [{"login": l, "display": d} for t, l, d in people if t == "APPROVED"]
    authors = [{"login": l, "display": d} for t, l, d in people if t == "AUTHORED"]
    milestones = sorted(({"stage": s, "release": v} for s, v in _q(
        "MATCH (:KEP {num:$n})-[r:REACHED]->(v:Release) RETURN r.stage, v.version", n=key)),
        key=lambda m: _minor(m["release"]))
    gates = [r[0] for r in _q("MATCH (:KEP {num:$n})-[:INTRODUCES]->(f:FeatureGate) RETURN f.name ORDER BY f.name", n=key)]

    ev = Evidence()
    for c in trail:
        ev.node(_kep_id(c["num"]), c["ref"], "KEP", title=c["title"], status=c["status"], created=c["created"])
    for a, b in links:
        ev.edge(_kep_id(a), _kep_id(b), "REPLACES")
    if kep["team"]:
        ev.node(_team_id(kep["team"]), kep["team"], "Team")
        ev.edge(_kep_id(key), _team_id(kep["team"]), "OWNED_BY")
    for name in gates[:3]:
        ev.node(_gate_id(name), name, "FeatureGate")
        ev.edge(_kep_id(key), _gate_id(name), "INTRODUCES")
    for p in approvers[:5]:
        ev.node(_person_id(p["login"]), p["display"], "Person")
        ev.edge(_person_id(p["login"]), _kep_id(key), "APPROVED")
    for s in see_also[:6]:
        ev.node(_kep_id(s["num"]), s["ref"], "KEP", title=s["title"], status=s["status"], created=s["created"])
        a, b = (key, s["num"]) if s["points_from_this_kep"] else (s["num"], key)
        ev.edge(_kep_id(a), _kep_id(b), "SEE_ALSO")

    def short(c):
        return f"{c['ref']} {c['title']} ({(c['created'] or '?')[:4]}, {c['status']})"
    parts = [f"{kep['ref']} '{kep['title']}' ({kep['team']}) is {kep['status']} at stage {kep['stage']}, "
             f"created {kep['created']}, latest milestone {kep['latest']}."]
    if replaces:
        parts.append("It replaces " + " and ".join(short(c) for c in replaces) + ".")
    if replaced_by:
        parts.append("It was replaced by " + " and ".join(short(c) for c in replaced_by) + ".")
    if milestones:
        parts.append("Milestones: " + ", ".join(f"{m['stage']} {m['release']}" for m in milestones) + ".")
    if approvers:
        parts.append("Approved by " + ", ".join(p["display"] for p in approvers) + ".")
    if see_also:
        parts.append(f"{len(see_also)} related KEPs (see-also).")
    if gate:
        parts.insert(0, f"Feature gate {gate} comes from {kep['ref']}.")
    sources = [_kep_source(c) for c in trail] + [_kep_source(s) for s in see_also[:6]]
    data = {"kep": kep, "feature_gate": gate, "matched_by": matched_by, "trail": trail,
            "replaces": replaces, "replaced_by": replaced_by, "see_also": see_also,
            "milestones": milestones, "approvers": approvers, "authors": authors, "feature_gates": gates}
    return _envelope("why", " ".join(parts), data, ev, sources)


def _minor(version):
    m = re.match(r"v?1\.(\d+)", str(version or ""))
    return int(m.group(1)) if m else -1


# ── whats_stale ─────────────────────────────────────────────────────────
def whats_stale(team=None, releases_behind=8):
    """KEPs still `implementable` at stage `alpha` whose latest milestone is 8 or more
    releases behind the newest one in the data. A candidate to check, not proof it is dead.
    data: {team, newest_release, cutoff_release, count, keps: [{num, ref, title, team,
           latest, created, url, authors}]} (oldest milestone first)"""
    if _use_stubs():
        return _stub("whats_stale")
    team_key = None
    if team:
        team_key = _find_team(str(team).removeprefix("team:"))
        if not team_key:
            return _not_found("whats_stale", team, "team")
    newest = _q("MATCH (k:KEP) RETURN max(k.latest_minor)")[0][0]
    cutoff = newest - releases_behind
    rows = _q(f"""MATCH (k:KEP)-[:OWNED_BY]->(t:Team)
                  WHERE k.status = 'implementable' AND k.stage = 'alpha'
                    AND k.latest_minor >= 0 AND k.latest_minor <= $cutoff
                    AND ($team IS NULL OR t.key = $team)
                  OPTIONAL MATCH (a:Person)-[:AUTHORED]->(k)
                  RETURN {KEP_FIELDS}, t.key, collect(a.login), k.latest_minor AS lm
                  ORDER BY lm, k.num""", cutoff=cutoff, team=team_key)
    keps = [{**_kep_row(r[:7], r[7]), "authors": sorted(r[8])} for r in rows]
    ev = Evidence()
    for k in keps:
        ev.node(_team_id(k["team"]), k["team"], "Team")
        if ev.node(_kep_id(k["num"]), k["ref"], "KEP", title=k["title"], latest=k["latest"]):
            ev.edge(_kep_id(k["num"]), _team_id(k["team"]), "OWNED_BY")
    where = f" owned by {team_key}" if team_key else ""
    summary = (f"{len(keps)} KEPs{where} are still implementable at alpha with their latest milestone "
               f"at v1.{cutoff} or older (newest release in the data: v1.{newest}).")
    if keps:
        summary += " Oldest: " + "; ".join(f"{k['ref']} {k['title']} ({k['team']}, {k['latest']})"
                                           for k in keps[:3]) + "."
    sources = [_kep_source(k) for k in keps[:MAX_NODES]]
    if team_key:
        sources.insert(0, _team_source(_team_info(team_key)))
    data = {"team": team_key, "newest_release": f"v1.{newest}", "cutoff_release": f"v1.{cutoff}",
            "count": len(keps), "keps": keps}
    return _envelope("whats_stale", summary, data, ev, sources)


# ── route_issue ─────────────────────────────────────────────────────────
def _stem(word):
    """Crude stemming so 'scheduled', 'scheduler' and 'scheduling' all become 'schedul'."""
    for suffix in ("ing", "ers", "er", "ed", "es", "s"):
        if len(word) >= 5 and word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[:-len(suffix)]
    if len(word) == 4 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]  # pods -> pod, jobs -> job
    return word


def _without_prefixes(words):
    """Drop a matched word when a longer matched word starts with it ('plac' and 'placement'
    both hit the title word 'Placement'; count it once, by the more specific word)."""
    return [w for w in words if not any(o != w and o.startswith(w) for o in words)]


def _words(text):
    """Distinct lowercase word stems of 3+ letters, without common words."""
    tokens = re.findall(r"[a-z0-9]{3,}", text.lower())
    return list(dict.fromkeys(_stem(t) for t in tokens if t not in STOPWORDS))[:60]


def _kep_matches(words, limit=10, weight=None):
    """KEPs whose title has a word starting with one of the stems (at most 4 letters longer:
    'schedul' matches 'scheduling', 'rest' doesn't match 'restarting'). Score = sum of
    log(KEP count / KEPs with that word), so rare words count more; `weight` multiplies it per word."""
    if not words:
        return []
    weight = weight or {}
    rows = _q("""MATCH (n:KEP) WITH count(n) AS total
                 UNWIND $words AS w
                 MATCH (k:KEP)
                 WITH total, w, k, split(replace(replace(replace(replace(replace(toLower(k.title),
                      '-', ' '), '(', ' '), ':', ' '), '/', ' '), ',', ' '), ' ') AS title_words
                 WHERE size([t IN title_words WHERE t STARTS WITH w AND size(t) <= size(w) + 4]) > 0
                 WITH total, w, collect(k.num) AS nums
                 RETURN w, log(toFloat(total) / size(nums)), nums""", words=words)
    idf, matched = {w: s for w, s, _ in rows}, {}
    for w, _, nums in rows:
        for num in nums:
            matched.setdefault(num, []).append(w)
    scored = {num: (sum(idf[w] * weight.get(w, 1) for w in _without_prefixes(ws)), _without_prefixes(ws))
              for num, ws in matched.items()}
    if not scored:
        return []
    info = {n: (t, team, c) for n, t, team, c in _q(
        """MATCH (k:KEP) WHERE k.num IN $nums OPTIONAL MATCH (k)-[:OWNED_BY]->(t:Team)
           RETURN k.num, trim(k.title), t.key, k.created""", nums=list(scored))}
    # Highest score first; ties go to the newer KEP.
    best = sorted(scored, key=lambda n: (scored[n][0], info[n][2] or ""), reverse=True)[:limit]
    return [{"num": n, "title": info[n][0], "team": info[n][1], "matched": scored[n][1],
             "score": round(scored[n][0], 2)} for n in best]


def _gate_matches(tokens):
    return [{"gate": g, "num": n, "title": t, "team": team} for g, n, t, team in _q(
        """MATCH (f:FeatureGate) WHERE toLower(f.name) IN $tokens
           MATCH (k:KEP)-[:INTRODUCES]->(f) OPTIONAL MATCH (k)-[:OWNED_BY]->(t:Team)
           RETURN f.name, k.num, trim(k.title), t.key""", tokens=tokens)]


def _folder_matches(words):
    """Folders with a path part starting with a stem (same 4-letter rule as KEP titles),
    and how many PRs of each team touch them."""
    if not words:
        return []
    rows = _q("""MATCH (n:Folder) WITH count(n) AS total
                 UNWIND $words AS w
                 MATCH (f:Folder)
                 WHERE size([s IN split(replace(replace(f.path, '-', '/'), '.', '/'), '/')
                             WHERE s STARTS WITH w AND size(s) <= size(w) + 4]) > 0
                 WITH total, w, collect(f) AS folders
                 WITH w, folders, log(toFloat(total) / size(folders)) AS idf
                 UNWIND folders AS f
                 MATCH (f)<-[:TOUCHES]-(pr:PR)-[:FOR_TEAM]->(t:Team)
                 RETURN w, idf, f.path, t.key, count(DISTINCT pr) AS prs""", words=words)
    return [{"word": w, "idf": idf, "folder": f, "team": t, "prs": n} for w, idf, f, t, n in rows]


def route_issue(title, body=""):
    """Suggest the owning team, related KEPs, 2-3 people and a draft comment for a new issue.
    Only graph queries: issue words are matched against KEP titles (rare words count more,
    title words count double), feature gate names (exact) and folder paths (which teams' PRs
    touch those folders).
    People come from who_knows on the best-matching KEP of that team, then on the team,
    preferring people active in the last 90 days.
    The draft names people without '@', so posting it never notifies real maintainers.
    data: {team, label, team_scores, related_keps, feature_gates, folders,
           suggested_people, draft_comment, matched_words}"""
    if _use_stubs():
        return _stub("route_issue")
    text = f"{title or ''}\n{body or ''}"
    words = _words(text)
    weight = {w: 2 if w in _words(title or "") else 1 for w in words}  # the title says the most
    tokens = list(dict.fromkeys(re.findall(r"[a-z0-9]{3,}", text.lower())))
    keps, gates, folders = _kep_matches(words, weight=weight), _gate_matches(tokens), _folder_matches(words)
    per_folder = {}
    for f in folders:
        per_folder.setdefault(f["folder"], []).append(f["word"])
    keep = {(path, w) for path, ws in per_folder.items() for w in _without_prefixes(list(set(ws)))}
    folders = [f for f in folders if (f["folder"], f["word"]) in keep]

    scores, reasons = {}, {}
    def vote(team, points, why_text):
        if team:
            scores[team] = scores.get(team, 0.0) + points
            reasons.setdefault(team, []).append(why_text)
    for k in keps:
        vote(k["team"], k["score"], f"KEP-{k['num']} title matches {', '.join(k['matched'])}")
    for g in gates:
        vote(g["team"], 5.0, f"feature gate {g['gate']} (KEP-{g['num']})")
    for w in {f["word"] for f in folders}:
        rows = [f for f in folders if f["word"] == w]
        total = sum(f["prs"] for f in rows)
        for team in {f["team"] for f in rows}:
            share = sum(f["prs"] for f in rows if f["team"] == team) / total
            vote(team, rows[0]["idf"] * weight.get(w, 1) * share,
                 f"folders matching '{w}' ({round(share * 100)}% of their PRs)")
    if not scores:
        return _envelope("route_issue", "No KEP title, feature gate or folder matched this issue's words.",
                         {"team": None, "matched_words": words, "draft_comment": ""}, Evidence(), [])
    team = max(scores, key=scores.get)
    team_info = _team_info(team)
    # Related: KEPs of matched feature gates, then the team's KEPs scoring at least half its best match.
    team_keps = [k for k in keps if k["team"] == team]
    best = team_keps[0]["score"] if team_keps else 0
    related_nums = list(dict.fromkeys([g["num"] for g in gates if g["team"] == team]
                                      + [k["num"] for k in team_keps if k["score"] >= best / 2]))[:3]
    related = [_kep_info(n) for n in related_nums]

    # People: worked on the best related KEP and active lately, then the team's most active.
    picked, chosen = [], set()
    pools = ([_knowers("kep", related[0]["num"])["ranked"]] if related else []) + [_knowers("team", team)["ranked"]]
    for pool in pools:
        for p in sorted(pool, key=lambda p: (-p["recent"], -p["total"])):
            if len(picked) < 3 and p["recent"] > 0 and p["login"] not in chosen:
                picked.append(p)
                chosen.add(p["login"])
    people = [{"login": p["login"], "display": _person_label(p), "recent": p["recent"], "total": p["total"],
               "is_official_owner": p["is_official_owner"], "on_this_kep": p.get("on_this_kep", []),
               "reason": (f"{p['total']} pieces of {team} evidence, {p['recent']} in the last {RECENT_DAYS} days"
                          + (f"; {'/'.join(p['on_this_kep'])} on {related[0]['ref']}" if p.get("on_this_kep") else "")
                          + ("; official owner" if p["is_official_owner"] else ""))} for p in picked]

    label = team.replace("-", "/", 1) if team.startswith(("sig-", "wg-")) else None
    lines = [f"Thanks for the report. This looks like it belongs to {team}"
             + (f" (label: {label})." if label else ".")]
    if related:
        lines += ["", "Related design decisions:"] + [f"- {k['ref']} {k['title']}: {k['url']}" for k in related]
    if people:
        lines += ["", "People who know this area (from their recent reviews and design work): "
                  + ", ".join(p["display"] for p in people) + "."]
    lines += ["", "Suggested by Lore from the Kubernetes graph; a person checks it before it is posted."]
    draft = "\n".join(lines)

    ev = Evidence()
    ev.node(_team_id(team), team, "Team")
    for k in related:
        ev.node(_kep_id(k["num"]), k["ref"], "KEP", title=k["title"])
        ev.edge(_kep_id(k["num"]), _team_id(team), "OWNED_BY")
    for g in gates:
        if g["num"] in related_nums and ev.node(_gate_id(g["gate"]), g["gate"], "FeatureGate"):
            ev.edge(_kep_id(g["num"]), _gate_id(g["gate"]), "INTRODUCES")
    for p in people:
        ev.node(_person_id(p["login"]), p["display"], "Person",
                status="owner_and_knower" if p["is_official_owner"] else "knower")
        for rel in p["on_this_kep"]:
            ev.edge(_person_id(p["login"]), _kep_id(related[0]["num"]), rel.upper())
    leads = {x["login"] for x in _leads(team)}
    for p in people:
        if p["login"] in leads:
            ev.edge(_person_id(p["login"]), _team_id(team), "LEADS")
    cut, no_cut, _, _ = _window(None)
    items = _recent_items("team", team, [p["login"] for p in people if not p["on_this_kep"]], cut, no_cut)
    sources = [_kep_source(k) for k in related] + [_team_source(team_info)]
    for login, its in items.items():
        for node_id, label_, ntype, rel, when, link in its[:1]:
            if ev.node(node_id, label_, ntype):
                ev.edge(_person_id(login), node_id, rel, at=when)
                ev.edge(node_id, _team_id(team), link)
                if ntype == "PR":
                    sources.append({"type": "PR", "ref": label_, "title": "kubernetes/kubernetes pull request",
                                    "url": PR_PAGE.format(node_id.split(":", 1)[1])})
    team_folders = sorted({(f["folder"], f["prs"]) for f in folders if f["team"] == team}, key=lambda x: -x[1])[:5]
    ranked_teams = sorted(scores.items(), key=lambda x: -x[1])[:3]
    data = {"team": team, "label": label,
            "team_scores": [{"team": t, "score": round(s, 2), "why": reasons[t][:5]} for t, s in ranked_teams],
            "related_keps": related, "feature_gates": [g for g in gates if g["team"] == team],
            "folders": [{"path": f, "team_prs": n} for f, n in team_folders],
            "suggested_people": people, "draft_comment": draft, "matched_words": words}
    summary = (f"Route to {team} (score {round(scores[team], 1)}"
               + (f"; next: {ranked_teams[1][0]} {round(ranked_teams[1][1], 1)}" if len(ranked_teams) > 1 else "")
               + "). " + (f"Related: {', '.join(k['ref'] + ' ' + k['title'] for k in related)}. " if related else "")
               + (f"Ask: {', '.join(p['display'] for p in people)}." if people else "No recently active people found."))
    return _envelope("route_issue", summary, data, ev, sources)


# ── stubs (USE_STUBS=1) ─────────────────────────────────────────────────
def _use_stubs():
    return os.getenv("USE_STUBS") == "1"


def _stub(tool):
    """The old fixed answers (app/main.py stubs and the tool contract example), in the tool envelope."""
    from app.main import get_stub_response
    stub = get_stub_response("gang scheduling" if tool == "why" else "Who actually knows SIG Node?")
    ids = {}
    nodes = []
    for n in stub["graph"]["nodes"]:
        key = n["id"].lower().removeprefix("kep-") if n["type"] == "KEP" else n["id"]
        ids[n["id"]] = f"{n['type'].lower()}:{key}"
        nodes.append({**n, "id": ids[n["id"]]})
    edges = [{**e, "source": ids[e["source"]], "target": ids[e["target"]]} for e in stub["graph"]["edges"]
             if e["source"] in ids and e["target"] in ids]
    data = {
        "who_owns": {"thing": {"type": "team", "key": "sig-node"}, "owners": stub.get("official_owners", [])},
        "who_knows": {"thing": {"type": "team", "key": "sig-node"}, "people": stub.get("actual_knowers", [])},
        "why": {"trail": stub.get("decision_trail", [])},
        "whats_stale": {"count": 25, "keps": []},
        "route_issue": {"team": "sig-node", "suggested_people": ["tallclair", "dchen1107"],
                        "draft_comment": "Issue verified. Routing to @sig-node-leads. Based on decision graph "
                                         "history, @tallclair and @dchen1107 are the primary subject matter experts."},
    }[tool]
    summary = {"whats_stale": "25 decisions are still implementable at alpha and untouched for 8+ releases.",
               "route_issue": "Route to sig-node; ask tallclair and dchen1107."}.get(tool, stub["answer"])
    return {"tool": tool, "summary": summary, "data": data, "evidence": {"nodes": nodes, "edges": edges},
            "sources": [{**s, "url": None} for s in stub.get("sources", [])], "is_stub": True}
