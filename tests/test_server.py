"""tests/test_server.py — Automated tests for Lore FastAPI server."""
import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app, rate_limiter

client = TestClient(app)


@pytest.fixture(autouse=True)
def stub_environment(monkeypatch):
    """Ensure server unit tests run with USE_STUBS=1."""
    monkeypatch.setenv("USE_STUBS", "1")


def test_index_page():
    """Verify GET / returns the frontend HTML."""
    response = client.get("/")
    assert response.status_code == 200
    assert "Lore" in response.text or "text/html" in response.headers.get("content-type", "")


def test_health_endpoint():
    """Verify GET /api/health returns ok: true and a boolean for falkordb."""
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True
    assert "falkordb" in data
    assert isinstance(data["falkordb"], bool)


def test_ask_endpoint_stub_response():
    """Verify POST /api/ask returns the verified stub structure with is_stub=true."""
    response = client.post("/api/ask", json={"question": "Who actually knows SIG Node?"})
    assert response.status_code == 200
    data = response.json()
    assert "answer" in data
    assert "official_owners" in data
    assert "actual_knowers" in data
    assert "sources" in data
    assert data.get("is_stub") is True


def test_ask_decision_trail():
    """Verify POST /api/ask returns decision trail for gang scheduling."""
    response = client.post("/api/ask", json={"question": "Why did they choose gang scheduling?"})
    assert response.status_code == 200
    data = response.json()
    assert "decision_trail" in data or "answer" in data
    assert data.get("is_stub") is True


def test_ask_rate_limiting(monkeypatch):
    """Verify rate limiter blocks client after 20 queries in a minute (behind our proxy)."""
    monkeypatch.setattr(main, "TRUST_PROXY", True)
    client_ip = "192.168.1.99"
    headers = {"x-forwarded-for": client_ip}

    # Reset limiter for test IP
    rate_limiter.requests[client_ip] = []

    # Make 20 allowed requests
    for i in range(20):
        resp = client.post("/api/ask", json={"question": f"Question {i}"}, headers=headers)
        assert resp.status_code == 200, f"Request {i} failed unexpectedly"

    # 21st request should be rejected with 429
    resp_blocked = client.post("/api/ask", json={"question": "Question 21"}, headers=headers)
    assert resp_blocked.status_code == 429
    err = resp_blocked.json()
    assert "Rate limit exceeded" in err.get("error", "")


def test_rate_limit_ignores_fake_forwarded_header():
    """A visitor can't dodge the limit by sending a different fake IP each time."""
    rate_limiter.requests.clear()
    codes = [
        client.post("/api/ask", json={"question": f"Q{i}"},
                    headers={"x-forwarded-for": f"10.0.0.{i}"}).status_code
        for i in range(21)
    ]
    assert codes[:20] == [200] * 20
    assert codes[20] == 429


def test_action_approve_endpoint():
    """Verify Team Member C's approve action router."""
    payload = {
        "action_id": "test_act_001",
        "action_type": "route_issue",
        "target_team": "sig-node",
        "suggested_assignees": ["tallclair", "dchen1107"],
        "comment_draft": "Routing to @tallclair based on KEP knowledge.",
        "approved_by": "test_user"
    }
    response = client.post("/api/actions/approve", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "approved"
    assert data["action_id"] == "test_act_001"
