"""app/actions.py — Team Member C's Action Router.

Implements the Approve button action, triage routing, and decision pings.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/actions", tags=["actions"])

# In-memory store for approved actions
_APPROVED_ACTIONS: List[Dict[str, Any]] = []


class ApproveActionRequest(BaseModel):
    action_id: Optional[str] = Field(default_factory=lambda: f"act_{int(datetime.now().timestamp())}")
    action_type: str = Field(default="route_issue", description="Type of action: route_issue, ping_stale_owners, approve_plan")
    target_team: Optional[str] = Field(default=None, description="Target team or SIG (e.g. sig-node)")
    suggested_assignees: Optional[List[str]] = Field(default_factory=list, description="Recommended experts/assignees")
    comment_draft: Optional[str] = Field(default=None, description="Draft response or triage comment")
    approved_by: Optional[str] = Field(default="maintainer", description="Approving user or admin")
    metadata: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Arbitrary action payload")


class ApproveActionResponse(BaseModel):
    status: str
    action_id: str
    applied_at: str
    message: str
    details: Dict[str, Any]


@router.post("/approve", response_model=ApproveActionResponse)
async def approve_action(req: ApproveActionRequest) -> ApproveActionResponse:
    """Approve a proposed recommendation, triage routing, or stale decision ping."""
    applied_at = datetime.now(timezone.utc).isoformat()
    record = {
        "action_id": req.action_id,
        "action_type": req.action_type,
        "target_team": req.target_team,
        "suggested_assignees": req.suggested_assignees,
        "comment_draft": req.comment_draft,
        "approved_by": req.approved_by,
        "applied_at": applied_at,
        "metadata": req.metadata,
    }
    _APPROVED_ACTIONS.append(record)

    msg = f"Action '{req.action_id}' ({req.action_type}) approved successfully."
    if req.target_team:
        msg += f" Routed to {req.target_team} with assignees {req.suggested_assignees}."

    return ApproveActionResponse(
        status="approved",
        action_id=req.action_id or "act_unknown",
        applied_at=applied_at,
        message=msg,
        details=record,
    )


@router.get("/history")
async def list_action_history() -> Dict[str, Any]:
    """Retrieve history of approved actions."""
    return {
        "count": len(_APPROVED_ACTIONS),
        "actions": list(reversed(_APPROVED_ACTIONS)),
    }
