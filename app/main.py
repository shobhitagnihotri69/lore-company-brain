"""app/main.py — Core FastAPI Server for Lore (Kubernetes Company Brain).

Endpoints:
- GET  /            -> Serves web/index.html
- POST /api/ask     -> Queries AI agent (or stub) with timeout & 20 req/min rate limit
- GET  /api/health  -> Returns {"ok": true, "falkordb": true/false} via RETURN 1
- /api/actions      -> Router from app/actions.py (Approve button action)
"""
import asyncio
from collections import defaultdict
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

# Load configuration from .env file
load_dotenv()

# `or` (not a getenv default) so that empty values like `FALKORDB_PORT=`
# from a fresh copy of .env.example fall back to the defaults instead of crashing.
FALKORDB_HOST = os.getenv("FALKORDB_HOST") or "localhost"
FALKORDB_PORT = int(os.getenv("FALKORDB_PORT") or "6379")
# Set TRUST_PROXY=1 only when the app sits behind our own reverse proxy.
TRUST_PROXY = os.getenv("TRUST_PROXY", "") == "1"
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "")

# Directory paths
BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
INDEX_HTML = WEB_DIR / "index.html"

# Initialize FastAPI application
app = FastAPI(
    title="Lore — Kubernetes Company Brain",
    description="FalkorDB-powered company brain mapping Kubernetes official owners vs actual knowers",
    version="0.1.0",
)

# Enable CORS for browser integration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include C's Action Router (Approve button actions)
try:
    from app import actions
    app.include_router(actions.router)
except ImportError:
    # Graceful fallback if actions is being worked on
    from fastapi import APIRouter
    fallback_router = APIRouter(prefix="/api/actions", tags=["actions"])
    app.include_router(fallback_router)

# Import A's agent if available
try:
    from app import agent
except ImportError:
    agent = None


# ── Thread-safe In-Memory Sliding Window Rate Limiter ───────────────
class SlidingWindowRateLimiter:
    """Limits visitors to 20 requests per 60 seconds per IP."""

    def __init__(self, max_requests: int = 20, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests: Dict[str, List[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def is_allowed(self, client_ip: str) -> bool:
        async with self._lock:
            now = time.time()
            cutoff = now - self.window_seconds
            # Evict timestamps outside the sliding window
            timestamps = [t for t in self.requests[client_ip] if t > cutoff]
            if len(timestamps) >= self.max_requests:
                self.requests[client_ip] = timestamps
                return False
            timestamps.append(now)
            self.requests[client_ip] = timestamps
            return True


rate_limiter = SlidingWindowRateLimiter(max_requests=20, window_seconds=60.0)


def get_client_ip(request: Request) -> str:
    """Find the visitor's IP for rate limiting.

    X-Forwarded-For is a header anyone can write, so by default we ignore it and
    use the real connection's IP. Only when TRUST_PROXY=1 (we run behind our own
    Caddy / Cloudflare proxy) do we read it, and then we take the LAST entry:
    that is the one our proxy added, so visitors can't fake it.
    """
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "127.0.0.1"


# ── Models ──────────────────────────────────────────────────────────
class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="Question about Kubernetes teams, decisions, or ownership")


# ── FalkorDB Health Check Helper ────────────────────────────────────
def check_falkordb_connection() -> bool:
    """Run RETURN 1 on FalkorDB to verify database health."""
    try:
        from falkordb import FalkorDB
        client = FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
        # Select graph and run test query
        g = client.select_graph("k8s")
        res = g.query("RETURN 1")
        return bool(res and res.result_set)
    except Exception:
        # Fallback to direct client ping if graph isn't loaded yet
        try:
            from falkordb import FalkorDB
            client = FalkorDB(host=FALKORDB_HOST, port=FALKORDB_PORT)
            if hasattr(client, "connection"):
                return bool(client.connection.ping())
            return False
        except Exception:
            return False


# ── Fallback Example Stub Responses ─────────────────────────────────
def get_stub_response(question: str) -> Dict[str, Any]:
    """Return verified stub responses from tool_contract.md with is_stub=True."""
    q_lower = question.lower()

    if "gang" in q_lower or "coscheduling" in q_lower or "schedule" in q_lower:
        return {
            "question": question,
            "answer": "Coscheduling (KEP-583) was originally proposed in July 2018 (provisional, last updated Jan 2019). It was superseded and replaced by Gang Scheduling (KEP-4671, September 2025) and Decouple PodGroup API (KEP-5832, January 2026). The current architecture decouples pod grouping from core scheduler logic.",
            "decision_trail": [
                {"kep": "KEP-583", "title": "Coscheduling", "date": "2018-07", "status": "provisional"},
                {"kep": "KEP-4671", "title": "Gang Scheduling", "date": "2025-09", "status": "implementable"},
                {"kep": "KEP-5832", "title": "Decouple PodGroup API", "date": "2026-01", "status": "implementable"},
            ],
            "official_owners": [
                {"login": "alculquicondor", "role": "chair", "team": "sig-scheduling"},
                {"login": "ahg-g", "role": "chair", "team": "sig-scheduling"}
            ],
            "actual_knowers": [
                {"login": "alculquicondor", "decisions_count": 48, "is_official": True},
                {"login": "ahg-g", "decisions_count": 39, "is_official": True},
                {"login": "sttts", "decisions_count": 14, "is_official": False}
            ],
            "sources": [
                {"type": "KEP", "ref": "KEP-583", "title": "Coscheduling"},
                {"type": "KEP", "ref": "KEP-4671", "title": "Gang Scheduling"},
                {"type": "KEP", "ref": "KEP-5832", "title": "Decouple PodGroup API"}
            ],
            "graph": {
                "nodes": [
                    {"id": "KEP-583", "label": "KEP-583: Coscheduling", "type": "KEP"},
                    {"id": "KEP-4671", "label": "KEP-4671: Gang Scheduling", "type": "KEP"},
                    {"id": "KEP-5832", "label": "KEP-5832: Decouple PodGroup API", "type": "KEP"}
                ],
                "edges": [
                    {"source": "KEP-4671", "target": "KEP-583", "relation": "REPLACES"},
                    {"source": "KEP-5832", "target": "KEP-4671", "relation": "REPLACES"}
                ]
            },
            "is_stub": True,
        }

    # Default / SIG Node stub answer
    return {
        "question": question,
        "answer": "While SIG Node has 5 official leaders (dchen1107, mrunalp, SergeyKanzhelev, derekwaynecarr, haircommander), the evidence from 679 design decisions shows that engineers tallclair (31 decisions), ffromani (21 decisions), and thockin (20 decisions) are among the top contributors despite not being on the official leadership roster.",
        "official_owners": [
            {"login": "dchen1107", "role": "chair", "team": "sig-node"},
            {"login": "mrunalp", "role": "chair", "team": "sig-node"},
            {"login": "SergeyKanzhelev", "role": "tech_lead", "team": "sig-node"},
            {"login": "derekwaynecarr", "role": "chair", "team": "sig-node"},
            {"login": "haircommander", "role": "tech_lead", "team": "sig-node"},
        ],
        "actual_knowers": [
            {"login": "dchen1107", "decisions_count": 63, "is_official": True},
            {"login": "mrunalp", "decisions_count": 62, "is_official": True},
            {"login": "SergeyKanzhelev", "decisions_count": 56, "is_official": True},
            {"login": "derekwaynecarr", "decisions_count": 43, "is_official": True},
            {"login": "haircommander", "decisions_count": 36, "is_official": True},
            {"login": "tallclair", "decisions_count": 31, "is_official": False},
            {"login": "ffromani", "decisions_count": 21, "is_official": False},
            {"login": "thockin", "decisions_count": 20, "is_official": False},
        ],
        "sources": [
            {"type": "KEP", "ref": "KEP-1287", "title": "In-Place Pod Vertical Scaling"},
            {"type": "KEP", "ref": "KEP-753", "title": "Node Resource Topology"},
            {"type": "REGISTRY", "ref": "sigs.yaml", "title": "SIG Node Leadership"},
        ],
        "graph": {
            "nodes": [
                {"id": "sig-node", "label": "SIG Node", "type": "Team"},
                {"id": "tallclair", "label": "tallclair", "type": "Person", "status": "knower"},
                {"id": "dchen1107", "label": "dchen1107", "type": "Person", "status": "owner_and_knower"},
            ],
            "edges": [
                {"source": "tallclair", "target": "sig-node", "relation": "KNOWS", "weight": 31},
                {"source": "dchen1107", "target": "sig-node", "relation": "LEADS", "weight": 63},
            ],
        },
        "is_stub": True,
    }


# ── Routes ──────────────────────────────────────────────────────────
@app.get("/", response_class=Response)
async def serve_index():
    """Serve web/index.html single-page frontend."""
    if not INDEX_HTML.exists():
        # Fallback if web/index.html is missing
        return JSONResponse(
            status_code=200,
            content={"message": "Lore Company Brain API is running. web/index.html not found.", "status": "active"},
        )
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/api/health")
async def health_check() -> Dict[str, Any]:
    """Check service health and FalkorDB connectivity (via RETURN 1)."""
    falkordb_alive = check_falkordb_connection()
    return {
        "ok": True,
        "falkordb": falkordb_alive,
    }


@app.post("/api/ask")
async def ask_brain(req: AskRequest, request: Request) -> JSONResponse:
    """Submit a query to the Lore AI Agent.

    - Rate limited to 20 queries/minute per client IP
    - Protected by a 60-second execution deadline
    - Returns structured JSON error on crash or timeout (never blank)
    - Returns stub response with is_stub=true until A's agent exists
    """
    client_ip = get_client_ip(request)

    # 1. Enforce rate limiting: 20 questions/minute
    allowed = await rate_limiter.is_allowed(client_ip)
    if not allowed:
        return JSONResponse(
            status_code=429,
            content={"error": "Rate limit exceeded. Maximum 20 questions per minute allowed."},
        )

    question = req.question.strip()
    if not question:
        return JSONResponse(
            status_code=400,
            content={"error": "Question cannot be empty."},
        )

    # 2. Invoke Agent with a 60-second timeout guard
    result = None
    try:
        if os.getenv("USE_STUBS") == "1":
            result = get_stub_response(question)
        elif agent and hasattr(agent, "ask") and callable(agent.ask):
            import inspect
            # Check if ask is an async coroutine or synchronous function
            if inspect.iscoroutinefunction(agent.ask):
                coro = agent.ask(question)
            else:
                coro = asyncio.to_thread(agent.ask, question)

            # Wait with a strict 60-second deadline
            result = await asyncio.wait_for(coro, timeout=60.0)
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=500,
            content={"error": "Agent execution timed out after 60 seconds."},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Agent execution failed: {str(e)}"},
        )

    # 3. Fallback to stub contract if agent returned None or is in progress
    if result is None:
        result = get_stub_response(question)

    return JSONResponse(status_code=200, content=result)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
