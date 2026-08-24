#!/usr/bin/env python3
"""
MCP Server for Zoom cloud recordings / meeting transcripts.

Lists recent Zoom cloud recordings, fetches transcript text for a given
meeting, and tracks which meetings have already been processed so a
periodic sync job doesn't redo work.

Authentication: Server-to-Server OAuth (account_credentials grant).
A fresh access token is requested per call (tokens last 1 hour; call volume
is typically low enough that there's no need to cache/refresh).
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

ZOOM_ACCOUNT_ID = os.environ.get("ZOOM_ACCOUNT_ID", "")
ZOOM_CLIENT_ID = os.environ.get("ZOOM_CLIENT_ID", "")
ZOOM_CLIENT_SECRET = os.environ.get("ZOOM_CLIENT_SECRET", "")
ZOOM_USER_ID = os.environ.get("ZOOM_USER_ID", "")  # email or userId of the recordings owner

# Where the "which meetings have already been processed" log lives.
#   "file" (default): a local JSON file. Works out of the box for any
#       deployer. On Railway, attach a small persistent volume at STATE_PATH
#       so it survives restarts/redeploys — without one it's ephemeral,
#       which just means a meeting or two might get reprocessed after a
#       redeploy, not a correctness problem.
#   "supabase": a Postgres table via Supabase's REST API (PostgREST), for
#       deployers who already have a Supabase project to point at. Table
#       schema: meeting_uuid text primary key, processed_at timestamptz,
#       note text.
STATE_BACKEND = os.environ.get("STATE_BACKEND", "file")
STATE_PATH = os.environ.get("STATE_PATH", os.path.join(os.path.dirname(__file__), "processed_meetings.json"))
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "zoom_processed_meetings")

if not all([ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, ZOOM_USER_ID]):
    raise RuntimeError(
        "ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, and ZOOM_USER_ID must be set in .env"
    )

if STATE_BACKEND == "supabase" and not (SUPABASE_URL and SUPABASE_KEY):
    raise RuntimeError("STATE_BACKEND=supabase requires SUPABASE_URL and SUPABASE_KEY to also be set")

# DNS-rebinding protection is enabled by default and checks the incoming
# Host header against an allowlist. That allowlist can't be hardcoded to a
# domain ahead of time (Railway's domain isn't known at build time, and
# every advisor who deploys this template gets a different one), so it's
# disabled here. Our own bearer-token auth (added below, in the entry
# point) is the real access control for the remote deployment; stdio/local
# runs are unaffected since this setting only matters for HTTP transport.
mcp = FastMCP(
    "zoom_mcp",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ---------------------------------------------------------------------------
# Auth + state helpers
# ---------------------------------------------------------------------------

def _raise_with_body(r: httpx.Response) -> None:
    """Like raise_for_status(), but includes Zoom's actual error body in the
    message — the default httpx error only has the URL and status code,
    which hides the useful part (Zoom's {"code":..., "message":...})."""
    if r.status_code >= 400:
        raise RuntimeError(f"Zoom API error {r.status_code} for {r.request.url}: {r.text}")


def _get_token() -> str:
    """Request a fresh Server-to-Server OAuth access token."""
    with httpx.Client(timeout=30) as client:
        r = client.post(
            "https://zoom.us/oauth/token",
            params={"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID},
            auth=(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET),
        )
        _raise_with_body(r)
        return r.json()["access_token"]


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}"}


def _ok(data) -> str:
    return json.dumps(data, indent=2, default=str)


def _load_state() -> dict:
    """Load the processed-meetings log from whichever backend is configured
    (STATE_BACKEND). Public tool functions never touch this directly — they
    call _load_state/_save_state and stay backend-agnostic."""
    if STATE_BACKEND == "supabase":
        with httpx.Client(timeout=15) as client:
            r = client.get(
                f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
                headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                params={"select": "meeting_uuid,processed_at,note"},
            )
            _raise_with_body(r)
            rows = r.json()
        processed = {
            row["meeting_uuid"]: {"processed_at": row.get("processed_at"), "note": row.get("note")}
            for row in rows
        }
        return {"processed": processed}

    if not os.path.exists(STATE_PATH):
        return {"processed": {}}
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"processed": {}}


def _save_state(state: dict) -> None:
    """Persist the full state dict. File backend: straight overwrite
    (unchanged behavior). Supabase backend: diff against what's currently
    stored and upsert/delete only the changed rows, since PostgREST has no
    "replace everything" primitive."""
    if STATE_BACKEND == "supabase":
        current = _load_state().get("processed", {})
        new = state.get("processed", {})
        with httpx.Client(timeout=15) as client:
            to_upsert = [
                {"meeting_uuid": uuid, "processed_at": v.get("processed_at"), "note": v.get("note")}
                for uuid, v in new.items()
                if uuid not in current or current[uuid] != v
            ]
            if to_upsert:
                r = client.post(
                    f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
                    headers={
                        "apikey": SUPABASE_KEY,
                        "Authorization": f"Bearer {SUPABASE_KEY}",
                        "Content-Type": "application/json",
                        "Prefer": "resolution=merge-duplicates",
                    },
                    json=to_upsert,
                )
                _raise_with_body(r)
            for uuid in current:
                if uuid not in new:
                    r = client.delete(
                        f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}",
                        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
                        params={"meeting_uuid": f"eq.{uuid}"},
                    )
                    _raise_with_body(r)
        return

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _vtt_to_text(vtt: str) -> str:
    """
    Convert a WEBVTT transcript into clean speaker-labeled lines.

    Zoom's cloud-recording transcript VTT format is roughly:
        1
        00:00:01.000 --> 00:00:04.500
        Speaker Name: What they said.

    Blank/index/timestamp lines are dropped; consecutive lines from the
    same speaker are left as separate lines (keeps it simple + faithful).
    """
    lines = []
    for raw_line in vtt.splitlines():
        line = raw_line.strip()
        if not line or line == "WEBVTT":
            continue
        if line.isdigit():
            continue
        if "-->" in line:
            continue
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Recordings
# ---------------------------------------------------------------------------

@mcp.tool()
def list_recent_recordings(days_back: int = 7) -> str:
    """
    List cloud recordings for the configured Zoom user within the last N days.

    Args:
        days_back: How many days back to search (default 7, max 30 per Zoom's
                   single-request window — call again with an older range for
                   more history).

    Returns each recording's meeting UUID, meeting ID, topic, start time, and
    whether a transcript file is available yet (transcripts can lag a few
    minutes behind the recording finishing).
    """
    days_back = min(days_back, 30)
    to_date = datetime.now(timezone.utc).date()
    from_date = to_date - timedelta(days=days_back)

    with httpx.Client(timeout=30) as client:
        r = client.get(
            f"https://api.zoom.us/v2/users/{ZOOM_USER_ID}/recordings",
            headers=_headers(),
            params={
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
                "page_size": 100,
            },
        )
        _raise_with_body(r)
        data = r.json()

    meetings = []
    for m in data.get("meetings", []):
        files = m.get("recording_files", [])
        has_transcript = any(f.get("file_type") == "TRANSCRIPT" for f in files)
        meetings.append({
            "uuid": m.get("uuid"),
            "meeting_id": m.get("id"),
            "topic": m.get("topic"),
            "start_time": m.get("start_time"),
            "duration_minutes": m.get("duration"),
            "has_transcript": has_transcript,
        })
    return _ok(meetings)


@mcp.tool()
def list_unprocessed_recordings(days_back: int = 7) -> str:
    """
    Same as list_recent_recordings, but filters out meetings already marked
    processed via mark_recording_processed. Use this in the periodic sync job
    instead of list_recent_recordings to avoid redoing work.

    Args:
        days_back: How many days back to search (default 7).
    """
    all_meetings = json.loads(list_recent_recordings(days_back))
    if isinstance(all_meetings, dict) and "error" in all_meetings:
        return _ok(all_meetings)
    state = _load_state()
    processed = state.get("processed", {})
    unprocessed = [m for m in all_meetings if m["uuid"] not in processed]
    return _ok(unprocessed)


@mcp.tool()
def get_meeting_transcript(meeting_uuid: str) -> str:
    """
    Fetch and return the transcript text for a single meeting.

    Args:
        meeting_uuid: The meeting UUID from list_recent_recordings /
                      list_unprocessed_recordings. If the UUID starts with a
                      '/' or contains '//', it will be double-URL-encoded
                      automatically (Zoom quirk for certain UUID formats).
    """
    from urllib.parse import quote

    encoded_uuid = meeting_uuid
    if meeting_uuid.startswith("/") or "//" in meeting_uuid:
        encoded_uuid = quote(quote(meeting_uuid, safe=""), safe="")
    else:
        encoded_uuid = quote(meeting_uuid, safe="")

    with httpx.Client(timeout=30) as client:
        r = client.get(
            f"https://api.zoom.us/v2/meetings/{encoded_uuid}/recordings",
            headers=_headers(),
        )
        _raise_with_body(r)
        data = r.json()

    files = data.get("recording_files", [])
    transcript_file = next((f for f in files if f.get("file_type") == "TRANSCRIPT"), None)
    if not transcript_file:
        return json.dumps({
            "error": "No transcript file found for this meeting yet. "
                     "It may still be processing — try again in a few minutes.",
            "topic": data.get("topic"),
            "start_time": data.get("start_time"),
        })

    download_url = transcript_file["download_url"]
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        token = _get_token()
        vtt_resp = client.get(download_url, headers={"Authorization": f"Bearer {token}"})
        _raise_with_body(vtt_resp)
        transcript_text = _vtt_to_text(vtt_resp.text)

    return _ok({
        "uuid": data.get("uuid"),
        "meeting_id": data.get("id"),
        "topic": data.get("topic"),
        "start_time": data.get("start_time"),
        "duration_minutes": data.get("duration"),
        "transcript": transcript_text,
    })


# ---------------------------------------------------------------------------
# Meeting Summaries (AI Companion / "My Notes")
#
# Separate from Cloud Recording. This is Zoom's AI Companion meeting-summary
# feature (what powers the "My Notes" panel in Zoom Hub). It does NOT require
# cloud recording to be turned on, so it can pick up meetings even when
# list_recent_recordings shows nothing. Tradeoff: it returns an AI-generated
# markdown ABSTRACT (key takeaways, discussed topics, action items) — not the
# verbatim per-speaker transcript that list_recent_recordings/
# get_meeting_transcript return from Cloud Recording.
#
# Requires the S2S OAuth app to have the meeting_summary:read:admin scope
# (granular: meeting:read:summary:admin) in addition to the cloud_recording
# scopes.
#
# KNOWN LIMITATION: like Cloud Recording, this is keyed to a single meeting
# instance. Two back-to-back meetings in the same Personal Meeting Room may
# get treated as ONE meeting instance and return ONE combined summary
# blending both calls — confirm this empirically before relying on it for
# back-to-back-meeting days.
# ---------------------------------------------------------------------------

@mcp.tool()
def list_recent_meeting_summaries(days_back: int = 7) -> str:
    """
    List AI Companion meeting summaries available for the configured Zoom
    user within the last N days (account-wide endpoint, filtered to
    ZOOM_USER_ID's meetings where possible).

    Args:
        days_back: How many days back to search (default 7, max 30).
    """
    days_back = min(days_back, 30)
    to_date = datetime.now(timezone.utc).date()
    from_date = to_date - timedelta(days=days_back)

    with httpx.Client(timeout=30) as client:
        r = client.get(
            "https://api.zoom.us/v2/meetings/meeting_summaries",
            headers=_headers(),
            params={
                "from": from_date.isoformat(),
                "to": to_date.isoformat(),
                "page_size": 100,
            },
        )
        _raise_with_body(r)
        data = r.json()

    return _ok(data)


@mcp.tool()
def get_meeting_summary(meeting_id: str) -> str:
    """
    Fetch the AI Companion-generated summary for a single meeting (markdown:
    key takeaways, discussed topics, challenges, action items). This is an
    abstract, not a verbatim transcript — use get_meeting_transcript instead
    if Cloud Recording is available for the meeting.

    Args:
        meeting_id: The meeting's numeric ID, or its UUID from
                    list_recent_meeting_summaries. If the UUID starts with a
                    '/' or contains '//', it will be double-URL-encoded
                    automatically (same Zoom quirk as get_meeting_transcript).
    """
    from urllib.parse import quote

    encoded_id = meeting_id
    if meeting_id.startswith("/") or "//" in meeting_id:
        encoded_id = quote(quote(meeting_id, safe=""), safe="")
    elif not meeting_id.isdigit():
        encoded_id = quote(meeting_id, safe="")

    with httpx.Client(timeout=30) as client:
        r = client.get(
            f"https://api.zoom.us/v2/meetings/{encoded_id}/meeting_summary",
            headers=_headers(),
        )
        _raise_with_body(r)
        data = r.json()

    return _ok({
        "meeting_uuid": data.get("meeting_uuid"),
        "meeting_id": data.get("meeting_id"),
        "meeting_topic": data.get("meeting_topic"),
        "meeting_start_time": data.get("meeting_start_time"),
        "meeting_end_time": data.get("meeting_end_time"),
        "summary_title": data.get("summary_title"),
        "summary_content": data.get("summary_content"),
        "next_steps": data.get("next_steps"),
        "summary_doc_url": data.get("summary_doc_url"),
    })


@mcp.tool()
def mark_recording_processed(meeting_uuid: str, note: Optional[str] = None) -> str:
    """
    Record that a meeting's transcript has been pulled and handed off to the
    post-meeting workflow, so future syncs skip it.

    Args:
        meeting_uuid: The meeting UUID that was processed.
        note: Optional free-text note (e.g. client name, meeting type) for
              easier debugging later.
    """
    state = _load_state()
    state.setdefault("processed", {})[meeting_uuid] = {
        "processed_at": datetime.now(timezone.utc).isoformat(),
        "note": note,
    }
    _save_state(state)
    return _ok({"status": "marked processed", "uuid": meeting_uuid})


@mcp.tool()
def unmark_recording_processed(meeting_uuid: str) -> str:
    """
    Remove a meeting from the processed log, e.g. to reprocess it after a
    bug fix.

    Args:
        meeting_uuid: The meeting UUID to unmark.
    """
    state = _load_state()
    removed = state.get("processed", {}).pop(meeting_uuid, None)
    _save_state(state)
    if removed is None:
        return json.dumps({"status": "not found in processed log", "uuid": meeting_uuid})
    return _ok({"status": "unmarked", "uuid": meeting_uuid})


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    port = os.environ.get("PORT")
    if port:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse, PlainTextResponse
        from starlette.routing import Route, Mount
        from contextlib import asynccontextmanager

        AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")

        class BearerAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                if AUTH_TOKEN:
                    if request.headers.get("authorization", "") != f"Bearer {AUTH_TOKEN}":
                        return JSONResponse({"error": "Unauthorized"}, status_code=401)
                return await call_next(request)

        async def health(request):
            # Deliberately unauthenticated: Railway's healthcheck has no way
            # to send the bearer token, and everything under mcp_app requires
            # it. This route is a sibling of mcp_app (not mounted under it),
            # so it can't become an auth bypass for anything else.
            return PlainTextResponse("ok")

        mcp_app = mcp.streamable_http_app()
        mcp_app.add_middleware(BearerAuthMiddleware)

        @asynccontextmanager
        async def combined_lifespan(_app):
            # Mount() does not forward ASGI lifespan events to the mounted
            # sub-app, and mcp_app's session manager only starts inside its
            # own lifespan (this is what actually starts its task group --
            # without it every /mcp request 500s with "Task group is not
            # initialized"). Manually entering it here on the outer app's
            # startup/shutdown keeps that working exactly as before.
            async with mcp_app.router.lifespan_context(mcp_app):
                yield

        app = Starlette(
            routes=[
                Route("/health", health),
                Mount("/", app=mcp_app),
            ],
            lifespan=combined_lifespan,
        )
        uvicorn.run(app, host="0.0.0.0", port=int(port))
    else:
        mcp.run(transport="stdio")
