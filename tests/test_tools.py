"""Tests for app/tools.py on the real `k8s` graph.

Skipped when FalkorDB isn't reachable or the graph isn't fully loaded (see README "Load the data").
Run: pytest -o pythonpath=. tests/test_tools.py
"""
import re

import pytest

from app import tools

SIG_NODE_LEADS = {"dchen1107", "derekwaynecarr", "haircommander", "mrunalp", "sergeykanzhelev"}
GANG_TITLE = "Gang scheduling: pods of a training job are only partly scheduled"
GANG_BODY = ("The scheduler places 5 of 8 pods of a job and the rest stay pending. "
             "We need all-or-nothing placement for the PodGroup.")
NODE_ID = re.compile(r"^(team|person|kep|pr|folder|gate):.+")


@pytest.fixture(scope="module", autouse=True)
def loaded_graph():
    try:
        folders = tools._q("MATCH (f:Folder)<-[:APPROVER_OF]-() RETURN count(f)")[0][0]
    except Exception as e:
        pytest.skip(f"FalkorDB not reachable: {e}")
    if not folders:
        pytest.skip("graph not loaded: run the loaders in README 'Load the data'")


@pytest.fixture(autouse=True)
def real_tools(monkeypatch):
    monkeypatch.delenv("USE_STUBS", raising=False)


def envelope(result, tool, stub=False):
    """Check the contract envelope and the evidence map; return `data`."""
    assert set(result) == {"tool", "summary", "data", "evidence", "sources", "is_stub"}
    assert result["tool"] == tool and result["is_stub"] is stub
    assert isinstance(result["summary"], str) and result["summary"]
    nodes, edges = result["evidence"]["nodes"], result["evidence"]["edges"]
    ids = [n["id"] for n in nodes]
    assert len(ids) == len(set(ids)), "duplicate node ids"
    assert len(nodes) <= tools.MAX_NODES
    for n in nodes:
        assert {"id", "label", "type"} <= set(n) and NODE_ID.match(n["id"]), n
    for e in edges:
        assert e["source"] in ids and e["target"] in ids, f"dangling edge {e}"
    for s in result["sources"]:
        assert {"type", "ref", "title", "url"} <= set(s)
        assert stub or s["url"].startswith("https://github.com/kubernetes/"), s
    return result["data"]


def test_who_owns_sig_node_returns_the_5_official_leaders():
    data = envelope(tools.who_owns("sig-node"), "who_owns")
    assert {o["login"] for o in data["owners"]} == SIG_NODE_LEADS
    assert all(set(o["roles"]) <= {"chair", "tech_lead"} for o in data["owners"])
    assert tools.who_owns("SIG Node")["data"]["owners"] == data["owners"]


def test_who_owns_kep_and_folder():
    kep = envelope(tools.who_owns("KEP-4671"), "who_owns")
    assert kep["team"] == "sig-scheduling" and kep["approvers"] and kep["team_leads"]
    folder = envelope(tools.who_owns("pkg/scheduler/framework/plugins/x.go"), "who_owns")
    assert folder["thing"]["path"] == "pkg/scheduler/framework"
    assert {"macsko", "dom4ha"} <= {a["login"] for a in folder["approvers"]}


def test_who_knows_sig_node():
    data = envelope(tools.who_knows("sig-node"), "who_knows")
    people = data["people"]
    assert people and people == sorted(people, key=lambda p: (-p["total"], -p["recent"], p["login"]))
    for p in people:
        assert p["total"] == sum(p["counts"].values()) and 0 <= p["recent"] <= p["total"]
        assert p["is_official_owner"] == (p["login"] in SIG_NODE_LEADS)
    assert {o["login"] for o in data["official_owners"]} == SIG_NODE_LEADS


def test_who_knows_as_of_uses_only_older_evidence():
    now = {p["login"]: p["total"] for p in tools.who_knows("sig-node")["data"]["people"]}
    result = tools.who_knows("sig-node", as_of="2026-01-01")
    data = envelope(result, "who_knows")
    assert data["as_of"] == "2026-01-01" and data["recent_window"]["from"] == "2025-10-03"
    for p in data["people"]:
        assert p["total"] <= now.get(p["login"], p["total"])
    dated = [e["at"] for e in result["evidence"]["edges"] if e.get("at")]
    assert dated and all(at < "2026-01-01" for at in dated)
    assert "as_of must be a date" in tools.who_knows("sig-node", as_of="soon")["summary"]


def test_who_knows_kep_lists_the_people_on_it():
    data = envelope(tools.who_knows("KEP-4671"), "who_knows")
    assert all(p["on_this_kep"] for p in data["people"])
    assert "macsko" in {p["login"] for p in data["people"]}


def test_why_kep_4671():
    data = envelope(tools.why("KEP-4671"), "why")
    assert data["kep"]["num"] == "4671" and data["kep"]["team"] == "sig-scheduling"
    assert {k["num"] for k in data["replaces"]} == {"583", "5832"}  # what 4671's kep.yaml says
    assert [k["num"] for k in data["trail"]][0] == "583"  # oldest first
    assert data["approvers"] and data["kep"]["url"].endswith("keps/sig-scheduling/4671-gang-scheduling")
    assert envelope(tools.why("GenericWorkload"), "why")["feature_gate"] == "GenericWorkload"


def test_whats_stale():
    data = envelope(tools.whats_stale(), "whats_stale")
    cutoff = int(data["cutoff_release"].split(".")[1])
    assert data["count"] == len(data["keps"]) > 0
    for k in data["keps"]:
        assert k["status"] == "implementable" and k["stage"] == "alpha"
        assert tools._minor(k["latest"]) <= cutoff and k["team"] and k["url"].startswith("https://")
    node = envelope(tools.whats_stale("sig-node"), "whats_stale")
    assert node["keps"] and all(k["team"] == "sig-node" for k in node["keps"])


def test_route_issue_gang_scheduling():
    data = envelope(tools.route_issue(GANG_TITLE, GANG_BODY), "route_issue")
    assert data["team"] == "sig-scheduling" and data["label"] == "sig/scheduling"
    assert data["related_keps"][0]["num"] == "4671"
    assert 2 <= len(data["suggested_people"]) <= 3
    assert "@" not in data["draft_comment"]  # never pings real maintainers
    assert "sig-scheduling" in data["draft_comment"] and "4671-gang-scheduling" in data["draft_comment"]


def test_unknown_thing():
    data = envelope(tools.who_owns("no-such-team-anywhere"), "who_owns")
    assert data["thing"] is None


@pytest.mark.parametrize("call", [
    lambda: tools.who_owns("sig-node"), lambda: tools.who_knows("sig-node"), lambda: tools.why("KEP-4671"),
    lambda: tools.whats_stale(), lambda: tools.route_issue(GANG_TITLE, GANG_BODY)])
def test_use_stubs_returns_the_old_stub_answers(call, monkeypatch):
    monkeypatch.setenv("USE_STUBS", "1")
    result = call()
    envelope(result, result["tool"], stub=True)
