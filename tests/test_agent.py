"""tests/test_agent.py — Offline test suite for Lore AI agent."""
import json
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from app import agent


class MockFunction:
    def __init__(self, name: str, arguments: dict):
        self.name = name
        self.arguments = json.dumps(arguments)


class MockToolCall:
    def __init__(self, call_id: str, name: str, arguments: dict):
        self.id = call_id
        self.type = "function"
        self.function = MockFunction(name, arguments)


class MockMessage:
    def __init__(self, content: str = None, tool_calls: List[MockToolCall] = None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls or []


class MockChoice:
    def __init__(self, message: MockMessage):
        self.message = message


class MockCompletion:
    def __init__(self, message: MockMessage):
        self.choices = [MockChoice(message)]


class ScriptedLLMClient:
    """Fake LLM client that returns scripted responses offline."""

    def __init__(self, responses: List[MockMessage]):
        self.responses = list(responses)
        self.call_history: List[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs) -> MockCompletion:
        self.call_history.append(kwargs)
        if kwargs.get("tool_choice") == "none":
            # Forced final answer
            return MockCompletion(MockMessage(content="Forced final answer without tools."))
        if self.responses:
            next_msg = self.responses.pop(0)
            return MockCompletion(next_msg)
        return MockCompletion(MockMessage(content="Default fallback answer."))


@pytest.fixture(autouse=True)
def stub_environment(monkeypatch):
    """Ensure all tests run offline with USE_STUBS=1."""
    monkeypatch.setenv("USE_STUBS", "1")
    monkeypatch.setenv("LLM_API_KEY", "fake_key_for_testing")


def test_calls_tool_then_answers():
    """Verify tool execution, steps recording, source numbering, and stub status."""
    script = [
        MockMessage(
            tool_calls=[MockToolCall("call_1", "who_owns", {"thing": "sig-node"})]
        ),
        MockMessage(
            content="While SIG Node has official leaders [1], others do significant work."
        ),
    ]
    client = ScriptedLLMClient(script)

    result = agent.ask("Who owns SIG Node?", client=client)

    assert result["question"] == "Who owns SIG Node?"
    assert len(result["steps"]) == 1
    assert result["steps"][0]["tool"] == "who_owns"
    assert result["steps"][0]["input"]["thing"] == "sig-node"
    assert result["is_stub"] is True
    assert len(result["sources"]) > 0
    assert result["sources"][0]["n"] == 1
    assert "[1]" in result["answer"]


def test_two_tool_calls_in_one_response():
    """Verify multi-tool responses run both tools and merge evidence without duplicate nodes/edges."""
    script = [
        MockMessage(
            tool_calls=[
                MockToolCall("call_1", "who_owns", {"thing": "sig-node"}),
                MockToolCall("call_2", "who_knows", {"thing": "sig-node"}),
            ]
        ),
        MockMessage(
            content="Comparing official owners [1] with active knowers [2] shows key differences."
        ),
    ]
    client = ScriptedLLMClient(script)

    result = agent.ask("Who owns vs knows SIG Node?", client=client)

    assert len(result["steps"]) == 2
    assert result["steps"][0]["tool"] == "who_owns"
    assert result["steps"][1]["tool"] == "who_knows"

    nodes = result["evidence"]["nodes"]
    edges = result["evidence"]["edges"]

    # Verify no duplicate nodes
    node_ids = [n["id"] for n in nodes]
    assert len(node_ids) == len(set(node_ids))

    # Verify no duplicate edges
    edge_keys = [(e["source"], e["target"], e.get("relation", "")) for e in edges]
    assert len(edge_keys) == len(set(edge_keys))


def test_answer_cites_nonexistent_source_and_renumbers():
    """Verify nonexistent citations like [9] are removed, uncited sources dropped, and remaining renumbered."""
    script = [
        MockMessage(
            tool_calls=[MockToolCall("call_1", "who_owns", {"thing": "sig-node"})]
        ),
        # Returns citation [9] (nonexistent) and citation [2] (real 2nd source in stub)
        MockMessage(
            content="Based on nonexistent data [9], and confirmed by leadership [2]."
        ),
    ]
    client = ScriptedLLMClient(script)

    result = agent.ask("Test citation cleanup", client=client)

    # Citation [9] must be removed
    assert "[9]" not in result["answer"]

    # Sources must be renumbered 1..k based on first citation
    assert len(result["sources"]) == 1
    assert result["sources"][0]["n"] == 1
    assert "[1]" in result["answer"]


def test_tool_raises_error_handled_gracefully(monkeypatch):
    """Verify that if a tool raises, error is passed back to model and ask() returns valid response."""
    def broken_who_owns(**kwargs):
        raise RuntimeError("FalkorDB connection refused")

    monkeypatch.setitem(agent.TOOL_CALLABLES, "who_owns", broken_who_owns)

    script = [
        MockMessage(
            tool_calls=[MockToolCall("call_1", "who_owns", {"thing": "sig-node"})]
        ),
        MockMessage(
            content="I encountered a database error checking SIG Node, so I checked our fallback records."
        ),
    ]
    client = ScriptedLLMClient(script)

    result = agent.ask("Who owns SIG Node?", client=client)

    assert len(result["steps"]) == 1
    assert "FalkorDB connection refused" in result["steps"][0]["summary"]
    assert "database error" in result["answer"]
    assert isinstance(result["evidence"]["nodes"], list)
    assert isinstance(result["evidence"]["edges"], list)


def test_stops_at_six_tool_calls_and_forces_answer():
    """Verify model execution loop stops at max 6 tool calls and calls with tool_choice='none'."""
    # Script that endlessly requests more tools
    endless_calls = [
        MockMessage(
            tool_calls=[MockToolCall(f"call_{i}", "who_owns", {"thing": "sig-node"})]
        )
        for i in range(10)
    ]
    client = ScriptedLLMClient(endless_calls)

    result = agent.ask("Infinite loop test", client=client)

    # Must cap at 6 tool calls
    assert len(result["steps"]) == 6
    # Last call must have enforced tool_choice="none"
    last_call = client.call_history[-1]
    assert last_call.get("tool_choice") == "none"
    assert result["answer"] == "Forced final answer without tools."


def test_every_edge_source_and_target_exist_in_nodes():
    """Verify graph integrity: every edge's source and target must exist in evidence nodes."""
    script = [
        MockMessage(
            tool_calls=[
                MockToolCall("call_1", "who_owns", {"thing": "sig-node"}),
                MockToolCall("call_2", "why", {"thing": "KEP-4671"}),
            ]
        ),
        MockMessage(
            content="SIG Node and KEP-4671 show the organizational and decision trail [1] [2]."
        ),
    ]
    client = ScriptedLLMClient(script)

    result = agent.ask("Graph integrity test", client=client)

    node_ids = {n["id"] for n in result["evidence"]["nodes"]}
    for edge in result["evidence"]["edges"]:
        assert edge["source"] in node_ids, f"Edge source {edge['source']} missing from nodes"
        assert edge["target"] in node_ids, f"Edge target {edge['target']} missing from nodes"
