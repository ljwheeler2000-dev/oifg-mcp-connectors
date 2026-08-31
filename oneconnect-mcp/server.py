#!/usr/bin/env python3
"""
MCP Server for OneConnect.pro — read-only wrapper over its Workspace API.

OneConnect.pro is a third-party CRM (built by HoosAI) used firm-wide at
OneIndiana/OneFlorida/OneInvest as the Salesforce replacement. This
connector wraps a per-advisor Personal Access Token (Settings -> API
Tokens) so an assistant can read pipeline/client data on the advisor's
behalf without holding their OneConnect password.

Authentication: static Bearer token, stored in .env next to this file.
No OAuth flow -- just an API key, same pattern as advisor-evolution-mcp.

READ-ONLY BY DESIGN, not by choice: as of the last verified check (see
README), OneConnect.pro's API does not yet expose write scopes for
accounts/prospects (accounts:write / prospects:write don't exist on any
token yet -- tracked as an open feature request on ticket OC-150 with the
vendor). This connector will only ever expose GET calls until that
changes. Anything that "writes" client data today (e.g. a meeting-notes
skill) should draft the text for the advisor to paste into OneConnect by
hand -- do not simulate a write here.

Known-flaky endpoints (confirmed against the vendor's own API, subject to
change -- re-verify against README before assuming these are still
accurate):
  - Working: {tenant}/accounts, {tenant}/prospects, /tasks, /meetings,
    /contacts.
  - Broken as of last check: /team, /reports, /analytics, /scrum all
    return 404 despite matching scopes existing on the token. /files
    intermittently 500s. /contracts, /invoices, /milestones, /emails
    scopes aren't offered by the token generator anymore (403 if tried).
  - Routing inconsistency: accounts/prospects are tenant-scoped
    ({tenant}/accounts), everything else is top-level (/tasks, not
    {tenant}/tasks).

Multi-tenant: every OIFG/OneFlorida/OneInvest office is a different
OneConnect tenant slug (e.g. "oneindiana", "onefloridafinancial" -- ask
your OneConnect admin if unsure, or check your OneConnect URL). This
connector reads ONECONNECT_TENANT_SLUG from .env -- never hardcode a
tenant slug here, since every advisor who deploys this template belongs
to a different one.
"""

import json
import os
from typing import Optional
from dotenv import load_dotenv
import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

OC_TOKEN = os.environ.get("ONECONNECT_TOKEN", "")
OC_BASE = os.environ.get("ONECONNECT_API_BASE", "https://oneconnect.pro/api")
OC_TENANT = os.environ.get("ONECONNECT_TENANT_SLUG", "")

if not OC_TOKEN:
    raise RuntimeError("ONECONNECT_TOKEN must be set in .env")
if not OC_TENANT:
    raise RuntimeError(
        "ONECONNECT_TENANT_SLUG must be set in .env -- this is your OneConnect "
        "office slug (e.g. 'oneindiana'), not a value to leave blank or guess. "
        "Check your OneConnect.pro URL or ask your admin."
    )

# DNS-rebinding protection is disabled here for the same reason as the
# other connectors in this repo: Railway's domain isn't known at build
# time, and every advisor who deploys this template gets a different one.
# Our own bearer-token auth (added below, in the entry point) is the real
# access control for the remote deployment.
mcp = FastMCP(
    "oneconnect",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

# Endpoints confirmed broken as of the last verified check (see README).
# Surfaced here so tool docstrings and check_connection() can warn callers
# instead of silently eating a 404/500.
KNOWN_BROKEN = ["/team", "/reports", "/analytics", "/scrum", "/files"]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _raise_with_body(r: httpx.Response) -> None:
    if r.status_code >= 400:
        raise RuntimeError(f"OneConnect API error {r.status_code} for {r.request.url}: {r.text}")


def _headers() -> dict:
    return {"Authorization": f"Bearer {OC_TOKEN}", "Accept": "application/json"}


def _ok(data) -> str:
    return json.dumps(data, indent=2, default=str)


def _get(path: str, params: Optional[dict] = None) -> dict:
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{OC_BASE}{path}", headers=_headers(), params=params)
        _raise_with_body(r)
        return r.json()


# ---------------------------------------------------------------------------
# Connection check
# ---------------------------------------------------------------------------

@mcp.tool()
def check_connection() -> str:
    """
    Confirm this OneConnect.pro connection is live and report which
    endpoints are currently working. OneConnect's API has a history of
    endpoints changing status between checks (see this server's module
    docstring) -- call this first in any session that will lean on
    OneConnect data, and again if a call unexpectedly fails, rather than
    assuming the last-known status still holds.
    """
    result = {"tenant": OC_TENANT, "base_url": OC_BASE, "endpoints": {}}
    checks = [
        (f"/{OC_TENANT}/accounts", "accounts"),
        (f"/{OC_TENANT}/prospects", "prospects"),
        ("/tasks", "tasks"),
        ("/meetings", "meetings"),
        ("/contacts", "contacts"),
    ]
    for path, label in checks:
        try:
            with httpx.Client(timeout=15) as client:
                r = client.get(f"{OC_BASE}{path}", headers=_headers(), params={})
            result["endpoints"][label] = f"{r.status_code} ok" if r.status_code < 400 else f"{r.status_code} error"
        except Exception as e:
            result["endpoints"][label] = f"error: {e}"
    result["known_broken_elsewhere"] = KNOWN_BROKEN
    result["note"] = "Write access (accounts:write / prospects:write) does not exist on the API yet -- read-only until that changes."
    return _ok(result)


# ---------------------------------------------------------------------------
# Accounts & prospects (tenant-scoped)
# ---------------------------------------------------------------------------

@mcp.tool()
def list_accounts() -> str:
    """
    List this tenant's OneConnect accounts (current/active clients and
    households) -- name, email, phone, status, and whatever other fields
    OneConnect returns. Read-only.
    """
    return _ok(_get(f"/{OC_TENANT}/accounts"))


@mcp.tool()
def list_prospects() -> str:
    """
    List this tenant's OneConnect prospects (pipeline, pre-client) --
    name, email, phone, status. Read-only.
    """
    return _ok(_get(f"/{OC_TENANT}/prospects"))


# ---------------------------------------------------------------------------
# Tasks, meetings, contacts (top-level -- NOT tenant-scoped, per OneConnect's
# own routing inconsistency)
# ---------------------------------------------------------------------------

@mcp.tool()
def list_tasks() -> str:
    """List tasks from OneConnect. Read-only. Top-level endpoint (not tenant-scoped)."""
    return _ok(_get("/tasks"))


@mcp.tool()
def list_meetings() -> str:
    """List meetings from OneConnect. Read-only. Top-level endpoint (not tenant-scoped)."""
    return _ok(_get("/meetings"))


@mcp.tool()
def list_contacts() -> str:
    """List contacts from OneConnect. Read-only. Top-level endpoint (not tenant-scoped)."""
    return _ok(_get("/contacts"))


# ===========================================================================
# Entry point (identical pattern to the other connectors in this repo)
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
        if not AUTH_TOKEN:
            raise RuntimeError(
                "MCP_AUTH_TOKEN is not set. Refusing to start a remote (Streamable "
                "HTTP) deployment without an auth token -- this connector exposes "
                "real client/prospect data, and running unauthenticated would "
                "leave it open to anyone with the URL. Set MCP_AUTH_TOKEN and "
                "redeploy."
            )

        class BearerAuthMiddleware(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
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
