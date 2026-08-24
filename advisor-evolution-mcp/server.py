#!/usr/bin/env python3
"""
MCP Server for Advisor Evolution (app.advisorevolution.io) — Workspace API.

Wraps a per-advisor "Connect my AI" API token from Advisor Evolution (AE
Settings -> Connected Apps) so an assistant can read/write pipeline,
business, training, and coaching data on the advisor's behalf without ever
holding their AE password. The token is scoped and revocable per-advisor —
nothing about this deployment is shared between different deployers.

Authentication: static Bearer token from AE Settings -> Connected Apps,
stored in .env next to this file. No OAuth flow — just an API key.

Guardrails (from Advisor Evolution's own connection instructions and its
OpenAPI description — treat as binding, not just a suggestion):
  - AE is the system of record for pipeline/business/training/coaching.
    Don't maintain a disconnected local duplicate.
  - Use AE's canonical relationship stages (see STAGE_VALUES below) and
    reference joint-work partners by id from
    GET /directory/joint-work-partners, never free text.
  - Never set aggregate News/Cases Open/business totals directly — AE
    derives them from relationship records.
  - Lifecycle fields (status/archived/outcome) are NOT settable via
    create/update — AE returns a 400 naming the right endpoint. Use
    advance_relationship_stage or archive_relationship instead.
  - Archiving is reversible (restorable in the AE app) but there is no
    hard delete — don't create throwaway/test relationships against a real
    account without a real plan to archive them afterward.
  - If AE and a local copy disagree, don't silently overwrite — surface
    the conflict to whoever's running this deployment.
  - Only ever act within this connection's granted scopes. Never attempt to
    access another Advisor Evolution user's data.

API surface (from GET /openapi.json):
  GET  /me
  GET  /relationships                        (?includeArchived=1, ?status=)
  POST /relationships                        (idempotent via externalId or
                                               Idempotency-Key header; ?dry_run=1)
  GET  /relationships/{id}
  PATCH /relationships/{id}                  (?dry_run=1; lifecycle fields 400)
  POST /relationships/{id}/advance           (stage progression)
  POST /relationships/{id}/archive           (the only way to "remove" one)
  GET  /directory/joint-work-partners
  GET  /business
  GET  /training
  POST /training/{assignmentId}/complete
  GET  /coaching
  GET  /coaching-export                      (needs org:read — not granted
                                               to per-advisor tokens; leadership only)
"""

import json
import os
from typing import Optional
from dotenv import load_dotenv
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

AE_TOKEN = os.environ.get("ADVISOR_EVOLUTION_TOKEN", "")
AE_BASE = os.environ.get("ADVISOR_EVOLUTION_API_BASE", "https://app.advisorevolution.io/api/v1")

if not AE_TOKEN:
    raise RuntimeError("ADVISOR_EVOLUTION_TOKEN must be set in .env")

# DNS-rebinding protection is enabled by default and checks the incoming
# Host header against an allowlist. That allowlist can't be hardcoded to a
# domain ahead of time (Railway's domain isn't known at build time, and
# every advisor who deploys this template gets a different one), so it's
# disabled here. Our own bearer-token auth (added below, in the entry
# point) is the real access control for the remote deployment; stdio/local
# runs are unaffected since this setting only matters for HTTP transport.
mcp = FastMCP(
    "advisor_evolution",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# Canonical stage progression, per AE's OpenAPI schema. Advance to
# "delivery" is what marks someone a placed client.
STAGE_VALUES = [
    "first", "collab", "planning_1", "planning_2", "planning_3",
    "underwriting", "close", "delivery",
]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _raise_with_body(r: httpx.Response) -> None:
    """Like raise_for_status(), but includes AE's actual error body. AE's
    400s name exactly what's wrong (e.g. an unsupported/lifecycle field) —
    surface that to the caller instead of a bare status code."""
    if r.status_code >= 400:
        raise RuntimeError(f"Advisor Evolution API error {r.status_code} for {r.request.url}: {r.text}")


def _headers() -> dict:
    return {"Authorization": f"Bearer {AE_TOKEN}", "Accept": "application/json"}


def _ok(data) -> str:
    return json.dumps(data, indent=2, default=str)


def _get(path: str, params: Optional[dict] = None) -> dict:
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{AE_BASE}{path}", headers=_headers(), params=params)
        _raise_with_body(r)
        return r.json()


def _patch(path: str, body: dict, params: Optional[dict] = None) -> dict:
    with httpx.Client(timeout=30) as client:
        r = client.patch(f"{AE_BASE}{path}", headers=_headers(), json=body, params=params)
        _raise_with_body(r)
        return r.json()


def _post(path: str, body: dict, params: Optional[dict] = None) -> dict:
    with httpx.Client(timeout=30) as client:
        r = client.post(f"{AE_BASE}{path}", headers=_headers(), json=body, params=params)
        _raise_with_body(r)
        return r.json()


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

@mcp.tool()
def whoami() -> str:
    """
    Confirm which Advisor Evolution account this connection is acting as:
    identity, role, office, mentor team, programs, and the exact scopes
    granted to this specific token. Call this first in any session that
    will touch this API, and any time behavior looks off (a scope mismatch
    usually means the token was rotated in AE Settings -> Connected Apps).
    """
    return _ok(_get("/me"))


# ---------------------------------------------------------------------------
# Relationships (pipeline)
# ---------------------------------------------------------------------------

@mcp.tool()
def list_relationships(include_archived: bool = False, status: Optional[str] = None) -> str:
    """
    List this advisor's Advisor Evolution relationships (their pipeline):
    id, name, connection (who referred them), stage/stageLabel, status, fyc,
    projectedFyc, expectedFyc (computed: projectedFyc x closeProbability%),
    closeProbability, projectedCloseMonth, jointWorkPartnerId/Name, intros,
    nextDate, archivedAt.

    Args:
        include_archived: by default archived relationships are excluded.
            Set True to include them (e.g. to audit what's been archived).
        status: optional filter (active | placed | lost | not-moving-forward
            | waiting | archived).

    Canonical stage values (see also STAGE_VALUES in this server): first,
    collab, planning_1, planning_2, planning_3, underwriting, close,
    delivery. "delivery" is what marks someone a placed client.
    """
    params = {}
    if include_archived:
        params["includeArchived"] = "1"
    if status:
        params["status"] = status
    return _ok(_get("/relationships", params=params))


@mcp.tool()
def get_relationship(relationship_id: str) -> str:
    """
    Get a single relationship by its Advisor Evolution id (from
    list_relationships).
    """
    return _ok(_get(f"/relationships/{relationship_id}"))


@mcp.tool()
def list_joint_work_partners() -> str:
    """
    List the people eligible to be named as a joint-work partner (other
    producing advisors/mentors in this advisor's organization — never
    themselves). Returns each person's canonical id — pass that as
    joint_work_partner_id on create_relationship/update_relationship.
    Free-text partner names are never accepted by AE.
    """
    return _ok(_get("/directory/joint-work-partners"))


@mcp.tool()
def create_relationship(
    name: str,
    connection: Optional[str] = None,
    stage: Optional[str] = None,
    joint_work_partner_id: Optional[str] = None,
    projected_fyc: Optional[float] = None,
    close_probability: Optional[float] = None,
    projected_close_month: Optional[str] = None,
    next_date: Optional[str] = None,
    external_id: Optional[str] = None,
    dry_run: bool = True,
) -> str:
    """
    Create a new relationship in Advisor Evolution — the canonical record,
    not a local duplicate. If external_id matches one already created, AE
    updates that record instead of duplicating (idempotent upsert) — always
    set external_id when syncing from another system so retries are safe.

    Guardrails (from AE's own connection instructions):
      - Use AE's canonical stage values (see STAGE_VALUES / list_relationships).
      - joint_work_partner_id must come from list_joint_work_partners —
        never a free-text partner name.
      - Never set aggregate business totals here — those are derived by AE
        from relationship records, not set directly.
      - Don't create throwaway/test relationships against a real account
        without archiving them afterward.

    Safety: dry_run defaults to True — AE validates and returns exactly
    what would be written without saving. Only pass dry_run=False after the
    preview has been reviewed and approved.
    """
    body = {
        "name": name,
        "connection": connection,
        "stage": stage,
        "jointWorkPartnerId": joint_work_partner_id,
        "projectedFyc": projected_fyc,
        "closeProbability": close_probability,
        "projectedCloseMonth": projected_close_month,
        "nextDate": next_date,
        "externalId": external_id,
    }
    body = {k: v for k, v in body.items() if v is not None}
    params = {"dry_run": "1"} if dry_run else None
    return _ok(_post("/relationships", body, params=params))


@mcp.tool()
def update_relationship(
    relationship_id: str,
    connection: Optional[str] = None,
    stage: Optional[str] = None,
    joint_work_partner_id: Optional[str] = None,
    projected_fyc: Optional[float] = None,
    close_probability: Optional[float] = None,
    projected_close_month: Optional[str] = None,
    next_date: Optional[str] = None,
    external_id: Optional[str] = None,
    dry_run: bool = True,
) -> str:
    """
    Update an existing relationship's fields (not lifecycle — see below).
    Writes to the real AE record, not a local copy.

    NOT settable here (AE returns 400 naming the right endpoint instead of
    silently ignoring it):
      - status / archived / outcome — use archive_relationship or
        advance_relationship_stage instead.

    Guardrails (from AE's own connection instructions):
      - Use AE's canonical stage values (see STAGE_VALUES).
      - joint_work_partner_id must come from list_joint_work_partners.
      - If local notes and AE disagree about this relationship, surface
        the conflict rather than silently overwriting AE's value.

    Safety: dry_run defaults to True — validates and previews without
    saving. Only pass dry_run=False after the preview has been reviewed and
    approved.
    """
    fields = {
        "connection": connection,
        "stage": stage,
        "jointWorkPartnerId": joint_work_partner_id,
        "projectedFyc": projected_fyc,
        "closeProbability": close_probability,
        "projectedCloseMonth": projected_close_month,
        "nextDate": next_date,
        "externalId": external_id,
    }
    fields = {k: v for k, v in fields.items() if v is not None}

    if not fields:
        return _ok({"error": "No fields provided to update."})

    params = {"dry_run": "1"} if dry_run else None
    return _ok(_patch(f"/relationships/{relationship_id}", fields, params=params))


@mcp.tool()
def advance_relationship_stage(relationship_id: str, stage: Optional[str] = None) -> str:
    """
    Move a relationship forward in the pipeline, stamping the
    reached-stage date. Omit `stage` to advance one step from wherever it
    currently is; pass a specific stage (see STAGE_VALUES) to jump there
    directly. Advancing to "delivery" is what marks someone a placed
    client.

    This has no dry-run option (not in AE's spec for this endpoint) — it's
    a real write every time. Confirm with the advisor before calling on a
    real (non-test) relationship if the outcome isn't obvious.
    """
    body = {"stage": stage} if stage else {}
    if stage and stage not in STAGE_VALUES:
        return _ok({"error": f"'{stage}' is not a recognized stage. Valid values: {STAGE_VALUES}"})
    return _ok(_post(f"/relationships/{relationship_id}/advance", body))


@mcp.tool()
def archive_relationship(relationship_id: str, reason: Optional[str] = None, confirm: bool = False) -> str:
    """
    Archive a relationship — the only way to "remove" one from the active
    pipeline (there is no hard delete). Reversible: the record and any
    placed production is preserved, and it can be restored in the AE app.
    Removes it from activePipelineCount, News/Cases counts, and
    projections.

    This IS a real destructive-ish action — preview + confirm before using
    it on anything that isn't obviously test data.

    Args:
        relationship_id: the relationship to archive.
        reason: optional note on why (stored on the record).
        confirm: must be True to actually archive. With confirm=False
            (default) this just fetches and returns the current record so
            it can be confirmed as the right one before archiving for real.
    """
    current = _get(f"/relationships/{relationship_id}").get("data", {})

    if not confirm:
        return _ok({
            "preview": True,
            "would_call": f"POST /relationships/{relationship_id}/archive",
            "relationship_name": current.get("name"),
            "current_status": current.get("status"),
            "note": "Preview only — nothing archived yet. Re-call with "
                    "confirm=True after review. This is reversible in the "
                    "AE app, but still get explicit confirmation first.",
        })

    body = {"reason": reason} if reason else {}
    return _ok(_post(f"/relationships/{relationship_id}/archive", body))


# ---------------------------------------------------------------------------
# Business (read-only — AE derives these from relationships)
# ---------------------------------------------------------------------------

@mcp.tool()
def get_business_snapshot() -> str:
    """
    This advisor's business snapshot: ytdFyc, projectedYearEndFyc,
    currentMonthExpectedFyc, nextMonthExpectedFyc, newsOut, casesOpen,
    openCaseCount, activePipelineCount, and funnel conversion stats.

    Read-only by design — AE derives these totals from relationship
    records; don't try to set them directly, and don't route around this
    via update_relationship either (it wouldn't work — these aren't
    writable relationship fields in the first place).
    """
    return _ok(_get("/business"))


# ---------------------------------------------------------------------------
# Training
# ---------------------------------------------------------------------------

@mcp.tool()
def list_training() -> str:
    """
    This advisor's Advisor Evolution training assignments: assignmentId,
    program, week, day, stage, readingPercent, audioPercent,
    worksheetSubmitted, applied, attended, completed.

    To mark one complete, use complete_training_assignment.
    """
    return _ok(_get("/training"))


@mcp.tool()
def complete_training_assignment(assignment_id: str, note: Optional[str] = None) -> str:
    """
    Mark one of this advisor's training assignments complete (assignment_id
    from list_training, e.g. "producer:33:monday"). This changes a real
    training record — confirm with the advisor before calling this for the
    first time on any assignment that isn't obviously a test.

    Args:
        assignment_id: e.g. "producer:33:monday" from list_training.
        note: optional applied-learning note.
    """
    body = {"note": note} if note else {}
    return _ok(_post(f"/training/{assignment_id}/complete", body))


# ---------------------------------------------------------------------------
# Coaching
# ---------------------------------------------------------------------------

@mcp.tool()
def get_coaching_status() -> str:
    """
    This advisor's released coaching status: whether this week's coaching
    has been released yet, and once released, week/version/health/news/
    cases/ytdFyc snapshot and coach commentary (whatsHappening,
    coachThisWeek).

    Read-only. Don't call GET /coaching-export — that requires org:read
    (minted only for Leadership/Admin per AE's own spec) and returns the
    WHOLE organization's data — out of scope here even if a future token
    happens to carry that scope, without checking first.
    """
    return _ok(_get("/coaching"))


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    port = os.environ.get("PORT")
    if port:
        import uvicorn
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")

        class BearerAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if AUTH_TOKEN:
                    if request.headers.get("authorization", "") != f"Bearer {AUTH_TOKEN}":
                        return JSONResponse({"error": "Unauthorized"}, status_code=401)
                return await call_next(request)

        app = mcp.streamable_http_app()
        app.add_middleware(BearerAuthMiddleware)
        uvicorn.run(app, host="0.0.0.0", port=int(port))
    else:
        mcp.run(transport="stdio")
