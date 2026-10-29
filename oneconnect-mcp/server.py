#!/usr/bin/env python3
"""
MCP Server for OneConnect.pro — mostly-read-only wrapper over its Workspace
API, with file upload/delete added where testing confirmed it actually works.

OneConnect.pro is a third-party CRM (built by HoosAI) used firm-wide at
OneIndiana/OneFlorida/OneInvest as the Salesforce replacement. This
connector wraps a per-advisor Personal Access Token (Settings -> API
Tokens) so an assistant can read pipeline/client data -- and now upload or
delete files -- on the advisor's behalf without holding their OneConnect
password.

Authentication: static Bearer token, stored in .env next to this file.
No OAuth flow -- just an API key, same pattern as advisor-evolution-mcp.

WRITE SUPPORT STATUS (last verified live against the vendor's API on
2026-09-01 -- re-verify before trusting this if it's been a while):
  - accounts:write / prospects:write do not exist as scopes on any token
    yet. Not a bug, just not built on OneConnect's side (tracked as an
    open feature request on ticket OC-176).
  - tasks:write, clients:write, meetings:write all have valid scopes and
    route to real endpoints, but every create attempt returns a clean
    500 (no partial record left behind) -- confirmed broken on
    OneConnect's backend, reported to the vendor with correlation IDs.
    RE-TESTED 2026-10-29: tasks and clients create + update now WORK.
    meetings create still 500s ("Failed to create meeting", with a
    correlationId). Task updates ignore account_id; DELETE /clients/{id}
    is 403 insufficient_scope (no delete scope); GET /tasks/{id} is 405.
  - files:write DOES work, but only as multipart/form-data -- a plain
    JSON POST to /files also 500s. delete (DELETE /files/{id}) works
    too. Both confirmed with a real create-then-delete round trip.
  - GET /files unfiltered 500s ("Failed to load results"), but
    GET /files?client_id=N works (2026-10-29). Uploads honor client_id
    (file lands under clients/N/) but ignore account_id.
  - List endpoints return 50 records per call; the list_* tools now
    follow paging (see _list_all) and report _paging.complete.

Meetings are still not writable: a meeting-notes skill should upload the
summary as a file linked with client_id instead, and never claim a
meeting record was written. OneConnect publishes an authenticated endpoint
catalog at GET /api/docs -- read it with api_get("/docs") when a path or
payload is in doubt.

Known-flaky endpoints (confirmed against the vendor's own API, subject to
change -- re-verify against README before assuming these are still
accurate):
  - Working: {tenant}/accounts, {tenant}/prospects, /tasks, /meetings,
    /contacts, /clients (returns real data, or an empty list if the
    office has no records there -- that's not an error).
  - Broken as of last check: /team, /reports, /analytics, /scrum all
    return 404 despite matching scopes existing on the token. /planner,
    /qa_automation, /qa_catalog also 404 on every path tried (2026-09-01)
    -- may just be wrong paths, may not exist at all; no vendor docs to
    check against. GET /files (list) 500s.
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

import base64
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
# instead of silently eating a 404/500. /files here specifically means the
# LIST endpoint -- get_file/upload_file/delete_file (single-record paths)
# work fine.
KNOWN_BROKEN = ["/team", "/reports", "/analytics", "/scrum", "/files (list)"]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

# Response headers worth quoting back to the vendor when something 500s.
_TRACE_HEADERS = ("x-request-id", "x-correlation-id", "x-vercel-id", "cf-ray", "x-amzn-requestid")


def _raise_with_body(r: httpx.Response) -> None:
    if r.status_code >= 400:
        trace = {h: r.headers[h] for h in _TRACE_HEADERS if h in r.headers}
        trace_txt = f" trace={trace}" if trace else ""
        raise RuntimeError(
            f"OneConnect API error {r.status_code} for {r.request.method} {r.request.url}: "
            f"{r.text[:2000]}{trace_txt}"
        )


def _headers() -> dict:
    return {"Authorization": f"Bearer {OC_TOKEN}", "Accept": "application/json"}


def _ok(data) -> str:
    return json.dumps(data, indent=2, default=str)


def _clean(d: Optional[dict]) -> dict:
    return {k: v for k, v in (d or {}).items() if v is not None}


def _get(path: str, params: Optional[dict] = None):
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{OC_BASE}{path}", headers=_headers(), params=_clean(params))
        _raise_with_body(r)
        return r.json()


def _send(method: str, path: str, body: dict):
    with httpx.Client(timeout=30) as client:
        r = client.request(method, f"{OC_BASE}{path}", headers=_headers(), json=body)
        _raise_with_body(r)
        try:
            return r.json()
        except Exception:
            return {"status": r.status_code, "body": r.text}


# ---------------------------------------------------------------------------
# Paging. OneConnect returns 50 records per call by default (accounts reports
# total=165 but returns 50) and publishes no docs on its paging params. So we
# try the common conventions in turn and keep whichever one actually yields
# new ids, deduping by id. If none do, the result says complete=False rather
# than pretending the first page is everything.
# ---------------------------------------------------------------------------

_PAGE_SIZE = 50
_MAX_PAGES = 60
_PAGING_STYLES = {
    # Documented in OneConnect's GET /api/docs catalog (2026-10-29): paged
    # endpoints take ?limit=&offset= and return X-Has-More. Try it first.
    "offset+limit": lambda i: {"offset": i * _PAGE_SIZE, "limit": _PAGE_SIZE},
    "page+limit": lambda i: {"page": i + 1, "limit": _PAGE_SIZE},
    "page+per_page": lambda i: {"page": i + 1, "per_page": _PAGE_SIZE},
    "page+pageSize": lambda i: {"page": i + 1, "pageSize": _PAGE_SIZE},
    "skip+take": lambda i: {"skip": i * _PAGE_SIZE, "take": _PAGE_SIZE},
}


def _split(data):
    """Return (records, total, list_key) for either a bare list or a {key: [...], total: n} envelope."""
    if isinstance(data, list):
        return data, None, None
    if isinstance(data, dict):
        total = data.get("total") if isinstance(data.get("total"), int) else data.get("count")
        for k, v in data.items():
            if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                return v, total if isinstance(total, int) else None, k
    return [], None, None


def _rid(rec: dict):
    return rec.get("id") or json.dumps(rec, sort_keys=True, default=str)


def _list_all(path: str, params: Optional[dict] = None, all_pages: bool = True):
    """Fetch a list endpoint, following pages when all_pages is set. Returns (envelope, records, meta)."""
    first = _get(path, params)
    records, total, key = _split(first)
    meta = {"returned": len(records), "total_reported": total, "paging": None, "complete": None}
    if not all_pages:
        meta["complete"] = (total is None or len(records) >= total) if total is not None else None
        return first, records, meta

    seen = {_rid(r) for r in records}
    merged = list(records)
    target = total
    # Without a reported total, only keep paging if the first page looks full.
    if (target is not None and len(merged) >= target) or (target is None and len(merged) < _PAGE_SIZE):
        meta["complete"] = True if target is not None else None
        return first, merged, meta

    for style, mk in _PAGING_STYLES.items():
        added_any = False
        for i in range(1, _MAX_PAGES):
            try:
                page = _get(path, {**(params or {}), **mk(i)})
            except RuntimeError:
                break
            recs, _, _ = _split(page)
            new = [r for r in recs if _rid(r) not in seen]
            if not new:
                break
            added_any = True
            for r in new:
                seen.add(_rid(r))
                merged.append(r)
            if target is not None and len(merged) >= target:
                break
        if added_any:
            meta["paging"] = style
            break

    meta["returned"] = len(merged)
    meta["complete"] = (len(merged) >= target) if target is not None else None
    return first, merged, meta


def _list_tool(path: str, params: Optional[dict], all_pages: bool) -> str:
    first, records, meta = _list_all(path, params, all_pages)
    _, _, key = _split(first)
    if key:
        out = dict(first)
        out[key] = records
    else:
        out = {"records": records}
    out["_paging"] = meta
    return _ok(out)


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

    Write support is NOT probed here (that would create records). Use the
    create_* tools on a clearly-labeled test record to re-verify writes.
    """
    result = {"tenant": OC_TENANT, "base_url": OC_BASE, "endpoints": {}}
    checks = [
        (f"/{OC_TENANT}/accounts", "accounts"),
        (f"/{OC_TENANT}/prospects", "prospects"),
        ("/tasks", "tasks"),
        ("/meetings", "meetings"),
        ("/contacts", "contacts"),
        ("/clients", "clients"),
        ("/files", "files (list)"),
    ]
    for path, label in checks:
        try:
            with httpx.Client(timeout=15) as client:
                r = client.get(f"{OC_BASE}{path}", headers=_headers(), params={})
            result["endpoints"][label] = f"{r.status_code} ok" if r.status_code < 400 else f"{r.status_code} error"
        except Exception as e:
            result["endpoints"][label] = f"error: {e}"
    result["known_broken_elsewhere"] = KNOWN_BROKEN
    result["write_support"] = {
        "accounts": "no write scope exists on the API yet",
        "prospects": "no write scope exists on the API yet",
        "tasks": "create/update WORK (2026-10-29); account_id link ignored on update; no GET by id",
        "clients": "create/update WORK (2026-10-29); delete is 403 (no scope)",
        "meetings": "create still 500s (vendor bug, correlationId returned); re-verify",
        "files": "upload/get/delete work; client_id link works, account_id ignored; list works only with ?client_id=",
    }
    return _ok(result)


# ---------------------------------------------------------------------------
# Accounts & prospects (tenant-scoped)
# ---------------------------------------------------------------------------

@mcp.tool()
def list_accounts(all_pages: bool = True, params: Optional[dict] = None) -> str:
    """
    List this tenant's OneConnect accounts (households) -- name, status,
    owner, phone, email. Read-only.

    all_pages (default True) follows paging so you get every record, not
    just the first 50; check `_paging.complete` in the result. `params` is
    passed through as query-string filters if you know one OneConnect
    honors (e.g. {"client_status": "active_client"}).
    """
    return _list_tool(f"/{OC_TENANT}/accounts", params, all_pages)


@mcp.tool()
def list_prospects(all_pages: bool = True, params: Optional[dict] = None) -> str:
    """
    List this tenant's OneConnect prospects (pipeline, pre-client) --
    name, email, phone, status. Read-only. Paging/params as list_accounts.
    """
    return _list_tool(f"/{OC_TENANT}/prospects", params, all_pages)


# ---------------------------------------------------------------------------
# Tasks, meetings, contacts, clients (top-level -- NOT tenant-scoped, per
# OneConnect's own routing inconsistency)
# ---------------------------------------------------------------------------

@mcp.tool()
def list_tasks(all_pages: bool = True, params: Optional[dict] = None) -> str:
    """List tasks from OneConnect. Read-only. Paging/params as list_accounts."""
    return _list_tool("/tasks", params, all_pages)


@mcp.tool()
def list_meetings(all_pages: bool = True, params: Optional[dict] = None) -> str:
    """List meetings from OneConnect. Read-only. Paging/params as list_accounts."""
    return _list_tool("/meetings", params, all_pages)


@mcp.tool()
def list_contacts(all_pages: bool = True, params: Optional[dict] = None) -> str:
    """List contacts (people, each linked to an account via account_id). Read-only. Paging/params as list_accounts."""
    return _list_tool("/contacts", params, all_pages)


@mcp.tool()
def list_clients(all_pages: bool = True, params: Optional[dict] = None) -> str:
    """
    List records from OneConnect's /clients endpoint -- these are pipeline
    records (stage, deal_value), e.g. the ones opened by the Advisor
    Evolution sync. They are a different object from accounts/contacts, so
    a person can exist here and nowhere else. Read-only. Paging/params as
    list_accounts.
    """
    return _list_tool("/clients", params, all_pages)


# ---------------------------------------------------------------------------
# Cross-object search. OneConnect's UI search doesn't cover every object
# type, and a person can live in clients, accounts, contacts or prospects
# with no link between them -- so look in all four.
# ---------------------------------------------------------------------------

_SEARCH_SOURCES = {
    "accounts": (lambda: f"/{OC_TENANT}/accounts",
                 ("account_name", "first_name", "last_name", "nickname", "email", "phone"),
                 ("id", "account_name", "client_status", "owner_name", "phone", "email")),
    "prospects": (lambda: f"/{OC_TENANT}/prospects",
                  ("name", "email", "phone"),
                  ("id", "name", "status", "owner_name", "phone", "email", "next_meeting_at")),
    "contacts": (lambda: "/contacts",
                 ("name", "email", "phone", "company"),
                 ("id", "name", "email", "phone", "account_id", "client_id")),
    "clients": (lambda: "/clients",
                ("company_name", "contact_name", "contact_email", "contact_phone"),
                ("id", "company_name", "stage", "deal_value", "contact_email", "contact_phone", "notes")),
}


def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def _matches(rec: dict, fields, query: str) -> bool:
    hay = " ".join(str(rec.get(f) or "") for f in fields).lower()
    q = query.strip().lower()
    qd = _digits(q)
    if len(qd) >= 7 and qd in _digits(hay):
        return True
    tokens = [t for t in q.replace(",", " ").split() if t]
    return bool(tokens) and all(t in hay for t in tokens)


@mcp.tool()
def find_person(query: str, sources: Optional[list[str]] = None) -> str:
    """
    Find a person/household by name, email, or phone across ALL OneConnect
    record types at once -- accounts, prospects, contacts and clients
    (pipeline). Use this instead of guessing which list someone is in.

    Name matching is case-insensitive and token-based ("courtney richards"
    matches "Richards, Courtney"). Phone matching ignores formatting when
    the query has 7+ digits. `sources` limits the search, e.g.
    ["clients", "contacts"]. Each hit is tagged with its source so you know
    where the record lives -- a hit only in "clients" means there's no
    household/contact record for that person yet.
    """
    wanted = sources or list(_SEARCH_SOURCES)
    out = {"query": query, "hits": [], "searched": {}}
    for name in wanted:
        if name not in _SEARCH_SOURCES:
            out["searched"][name] = "unknown source"
            continue
        path_fn, match_fields, keep = _SEARCH_SOURCES[name]
        try:
            _, records, meta = _list_all(path_fn(), None, True)
            out["searched"][name] = meta
        except Exception as e:
            out["searched"][name] = f"error: {e}"
            continue
        for rec in records:
            if _matches(rec, match_fields, query):
                hit = {"source": name}
                hit.update({k: rec.get(k) for k in keep if k in rec})
                out["hits"].append(hit)
    out["hit_count"] = len(out["hits"])
    return _ok(out)


# ---------------------------------------------------------------------------
# Read-only probe -- for testing token scopes against paths this connector
# has no dedicated tool for (planner, reports, analytics, team, scrum,
# qa_*). GET only, so it can never change data.
# ---------------------------------------------------------------------------

@mcp.tool()
def api_get(path: str, params: Optional[dict] = None, max_chars: int = 4000, body_offset: int = 0) -> str:
    """
    Make a read-only GET against any OneConnect API path and return the
    status and (truncated) body. Use it to test whether a scope on the
    token (planner, reports, analytics, team, scrum, qa_*) has a live
    endpoint, or to discover query params. `{tenant}` in the path is
    replaced with this connector's tenant slug, e.g. "/{tenant}/planner".
    GET only -- this tool cannot create, change or delete anything.

    Long bodies (e.g. "/docs", OneConnect's authenticated endpoint
    catalog) are returned in slices: raise max_chars (up to 60000) or page
    through with body_offset; `next_offset` says where the next slice
    starts.
    """
    if not path.startswith("/") or ".." in path or "://" in path:
        raise ValueError("path must be an API-relative path like '/reports' or '/{tenant}/planner'")
    path = path.replace("{tenant}", OC_TENANT)
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{OC_BASE}{path}", headers=_headers(), params=_clean(params))
    try:
        body = r.json()
        preview = json.dumps(body, default=str)
    except Exception:
        preview = r.text
    max_chars = max(1, min(int(max_chars), 60000))
    start = max(0, int(body_offset))
    end = start + max_chars
    paging_headers = {k: v for k, v in r.headers.items() if k.lower().startswith("x-")}
    return _ok({
        "status": r.status_code,
        "url": str(r.request.url),
        "x_headers": paging_headers,
        "body_length": len(preview),
        "body_offset": start,
        "body_preview": preview[start:end],
        "truncated": len(preview) > end,
        "next_offset": end if len(preview) > end else None,
    })


# ---------------------------------------------------------------------------
# Writes for tasks / meetings / clients. The scopes exist on the token; the
# last live test (2026-09-01) saw OneConnect 500 on every create. These tools
# exist so that can be re-verified and so they start working the day the
# vendor fixes it. Errors come back with the vendor's body and any trace
# headers so they can go straight into a support ticket.
# ---------------------------------------------------------------------------

@mcp.tool()
def create_task(
    title: str,
    description: Optional[str] = None,
    due_date: Optional[str] = None,
    priority: Optional[str] = None,
    status: Optional[str] = None,
    account_id: Optional[str] = None,
    client_id: Optional[int] = None,
    prospect_id: Optional[str] = None,
    assigned_to: Optional[str] = None,
) -> str:
    """
    Create a OneConnect task. due_date is ISO 8601 (e.g. "2026-11-05");
    priority is low/medium/high; status defaults to OneConnect's own
    default (pending). assigned_to is a OneConnect user id -- omit to let
    OneConnect assign it to the token's owner.
    Verified working 2026-10-29. "low" priority came back as "medium" --
    OneConnect's priority values may differ; check the catalog (api_get "/docs").
    """
    return _ok(_send("POST", "/tasks", _clean({
        "title": title, "description": description, "due_date": due_date,
        "priority": priority, "status": status, "account_id": account_id,
        "client_id": client_id, "prospect_id": prospect_id, "assigned_to": assigned_to,
    })))


@mcp.tool()
def update_task(task_id: int, fields: dict) -> str:
    """Update a OneConnect task (PATCH). `fields` e.g. {"status": "completed"} or {"due_date": "2026-11-12"}."""
    return _ok(_send("PATCH", f"/tasks/{task_id}", fields))


@mcp.tool()
def create_meeting(
    title: str,
    meeting_date: str,
    duration_minutes: Optional[int] = None,
    status: Optional[str] = None,
    description: Optional[str] = None,
    notes: Optional[str] = None,
    transcript_summary: Optional[str] = None,
    action_items: Optional[list] = None,
    account_id: Optional[str] = None,
    client_id: Optional[int] = None,
    prospect_id: Optional[str] = None,
) -> str:
    """
    Create a OneConnect meeting record -- e.g. to file a meeting summary
    against a household instead of as a loose File Cabinet document.
    meeting_date is ISO 8601 with offset (e.g. "2026-10-29T14:00:00-04:00").
    Still 500s as of 2026-10-29 ("Failed to create meeting" + correlationId)
    -- vendor bug. Fall back to upload_file with client_id.
    """
    return _ok(_send("POST", "/meetings", _clean({
        "title": title, "meeting_date": meeting_date, "duration_minutes": duration_minutes,
        "status": status, "description": description, "notes": notes,
        "transcript_summary": transcript_summary, "action_items": action_items,
        "account_id": account_id, "client_id": client_id, "prospect_id": prospect_id,
    })))


@mcp.tool()
def update_meeting(meeting_id: int, fields: dict) -> str:
    """Update a OneConnect meeting (PATCH), e.g. {"notes": "...", "transcript_summary": "..."}."""
    return _ok(_send("PATCH", f"/meetings/{meeting_id}", fields))


@mcp.tool()
def create_client(
    company_name: str,
    contact_name: Optional[str] = None,
    contact_email: Optional[str] = None,
    contact_phone: Optional[str] = None,
    stage: Optional[str] = None,
    deal_value: Optional[float] = None,
    notes: Optional[str] = None,
    next_review_date: Optional[str] = None,
) -> str:
    """
    Create a OneConnect /clients (pipeline) record. stage is one of
    discovery/proposal/underwriting/issued/lost. Verified working
    2026-10-29, as is update_client (stage, deal_value, next_review_date).
    There is no delete for clients (403); set stage "lost" instead.
    """
    return _ok(_send("POST", "/clients", _clean({
        "company_name": company_name, "contact_name": contact_name or company_name,
        "contact_email": contact_email, "contact_phone": contact_phone, "stage": stage,
        "deal_value": deal_value, "notes": notes, "next_review_date": next_review_date,
    })))


@mcp.tool()
def update_client(client_id: int, fields: dict) -> str:
    """Update a OneConnect /clients record (PATCH), e.g. {"stage": "issued"} or {"next_review_date": "2027-04-01"}."""
    return _ok(_send("PATCH", f"/clients/{client_id}", fields))


@mcp.tool()
def delete_record(kind: str, record_id: int, confirm_title: str) -> str:
    """
    Permanently delete a meeting -- intended for removing test records.
    kind must be "meetings". confirm_title must exactly equal the record's
    title, as a guard against deleting the wrong id. No undo.

    Tasks can't be deleted: the catalog has no DELETE /tasks/{id} and the
    call returns 403 insufficient_scope (live-tested 2026-10-29). Mark a
    stray task completed with update_task instead. Clients can't be
    deleted or archived either (both 403) -- set their stage to "lost"
    with update_client.
    """
    if kind == "tasks":
        raise ValueError("tasks can't be deleted via the API (403); use update_task(id, {'status': 'completed'})")
    if kind != "meetings":
        raise ValueError("kind must be meetings (tasks/clients have no delete scope)")
    _, records, _ = _list_all(f"/{kind}", None, True)
    rec = next((r for r in records if str(r.get("id")) == str(record_id)), None)
    if rec is None:
        raise ValueError(f"{kind} record {record_id} not found; nothing deleted")
    if rec.get("title") != confirm_title:
        raise ValueError(f"confirm_title mismatch: record {record_id} is titled {rec.get('title')!r}; nothing deleted")
    return _ok(_send("DELETE", f"/{kind}/{record_id}", {}))


# ---------------------------------------------------------------------------
# Full catalog coverage (from GET /api/docs, 2026-10-29). Request bodies are
# not documented by OneConnect, so each write takes the common named fields
# plus `extra` (merged into the JSON body) for anything else. Errors return
# the vendor's body so a wrong field name is visible immediately.
# ---------------------------------------------------------------------------

def _body(named: dict, extra: Optional[dict]) -> dict:
    return {**_clean(named), **(extra or {})}


# --- Clients: single-record reads and pipeline actions ---------------------

@mcp.tool()
def get_client(client_id: int) -> str:
    """Fetch one /clients (pipeline) record by id."""
    return _ok(_get(f"/clients/{client_id}"))


@mcp.tool()
def move_client_stage(client_id: int, stage: str, extra: Optional[dict] = None) -> str:
    """
    Move a client's pipeline stage via POST /clients/{id}/stage
    (discovery/proposal/underwriting/issued/lost). update_client with
    {"stage": ...} also works; this is OneConnect's dedicated route.
    """
    return _ok(_send("POST", f"/clients/{client_id}/stage", _body({"stage": stage}, extra)))


@mcp.tool()
def archive_client(client_id: int, extra: Optional[dict] = None) -> str:
    """
    Archive a client (POST /clients/{id}/archive). Returns 403
    insufficient_scope for an advisor token (live-tested 2026-10-29) --
    to retire a pipeline record, use update_client(id, {"stage": "lost"}).
    """
    return _ok(_send("POST", f"/clients/{client_id}/archive", _body({}, extra)))


@mcp.tool()
def reassign_client_rep(client_id: int, rep_id: str, extra: Optional[dict] = None) -> str:
    """Reassign a client's rep (PATCH /clients/{id}/rep). rep_id is a user id from list_users."""
    return _ok(_send("PATCH", f"/clients/{client_id}/rep", _body({"assigned_rep_id": rep_id, "rep_id": rep_id}, extra)))


@mcp.tool()
def list_client_shares(client_id: int) -> str:
    """Who a client record is shared with (e.g. joint-work partners)."""
    return _ok(_get(f"/clients/{client_id}/shares"))


@mcp.tool()
def share_client(client_id: int, user_id: str, extra: Optional[dict] = None) -> str:
    """Share a client record with another OneConnect user (POST /clients/{id}/shares), e.g. a JWP. user_id from list_users."""
    return _ok(_send("POST", f"/clients/{client_id}/shares", _body({"user_id": user_id}, extra)))


@mcp.tool()
def unshare_client(client_id: int, user_id: str) -> str:
    """Remove a user's share on a client record."""
    return _ok(_send("DELETE", f"/clients/{client_id}/shares/{user_id}", {}))


@mcp.tool()
def list_client_portal_users(client_id: int) -> str:
    """The client's portal users (GET /clients/{id}/users)."""
    return _ok(_get(f"/clients/{client_id}/users"))


# --- Contacts ---------------------------------------------------------------

@mcp.tool()
def create_contact(
    name: str,
    client_id: Optional[int] = None,
    account_id: Optional[str] = None,
    email: Optional[str] = None,
    phone: Optional[str] = None,
    title: Optional[str] = None,
    company: Optional[str] = None,
    notes: Optional[str] = None,
    dob: Optional[str] = None,
    extra: Optional[dict] = None,
) -> str:
    """
    Create a OneConnect contact (a person). Link it to a pipeline record
    with client_id and/or a household with account_id -- this is how a
    pipeline-only person (e.g. an Advisor Evolution-synced client) becomes
    findable as a contact. dob is YYYY-MM-DD.

    KNOWN VENDOR BUG (2026-10-29): POST /contacts returns 500 "Failed to
    create contact" even with only {"name"} -- not a field-name issue.
    """
    return _ok(_send("POST", "/contacts", _body({
        "name": name, "client_id": client_id, "account_id": account_id, "email": email,
        "phone": phone, "title": title, "company": company, "notes": notes, "dob": dob,
    }, extra)))


@mcp.tool()
def update_contact(contact_id: int, fields: dict) -> str:
    """Update a contact (PATCH /contacts/{id}), e.g. {"email": "...", "client_id": 24583}."""
    return _ok(_send("PATCH", f"/contacts/{contact_id}", fields))


@mcp.tool()
def delete_contact(contact_id: int, confirm_name: str) -> str:
    """Permanently delete a contact. confirm_name must exactly match the contact's name. No undo."""
    _, records, _ = _list_all("/contacts", None, True)
    rec = next((r for r in records if str(r.get("id")) == str(contact_id)), None)
    if rec is None:
        raise ValueError(f"contact {contact_id} not found; nothing deleted")
    if rec.get("name") != confirm_name:
        raise ValueError(f"confirm_name mismatch: contact {contact_id} is {rec.get('name')!r}; nothing deleted")
    return _ok(_send("DELETE", f"/contacts/{contact_id}", {}))


# --- Notes & next steps -----------------------------------------------------

@mcp.tool()
def list_notes(client_id: int) -> str:
    """Notes on a client record (GET /notes?client_id=)."""
    return _ok(_get("/notes", {"client_id": client_id}))


@mcp.tool()
def create_note(client_id: int, content: str, title: Optional[str] = None, extra: Optional[dict] = None) -> str:
    """
    Add a note to a client record -- e.g. a meeting summary, since
    create_meeting still 500s. Sent as {"client_id", "content", "title"};
    if OneConnect wants a different field name, pass it via extra.

    KNOWN VENDOR BUG (2026-10-29): POST /notes returns 500 "Failed to
    create note" (with or without title). Use upload_file_scanned with
    client_id for meeting summaries until OneConnect fixes it.
    """
    return _ok(_send("POST", "/notes", _body({
        "client_id": client_id, "content": content, "title": title,
    }, extra)))


@mcp.tool()
def update_note(note_id: int, fields: dict) -> str:
    """Update a note (PATCH /notes/{id})."""
    return _ok(_send("PATCH", f"/notes/{note_id}", fields))


@mcp.tool()
def delete_note(note_id: int) -> str:
    """Permanently delete a note (DELETE /notes/{id}). No undo."""
    return _ok(_send("DELETE", f"/notes/{note_id}", {}))


@mcp.tool()
def list_next_steps(client_id: int) -> str:
    """Next-step entries for a client (GET /next-steps?client_id= -- client_id is required)."""
    return _ok(_get("/next-steps", {"client_id": client_id}))


# --- Meetings: single read / delete ----------------------------------------

@mcp.tool()
def get_meeting(meeting_id: int) -> str:
    """Fetch one meeting by id."""
    return _ok(_get(f"/meetings/{meeting_id}"))


# --- Subtasks & checklists ---------------------------------------------------

@mcp.tool()
def list_subtasks(task_id: int) -> str:
    """Subtasks of a task (GET /subtasks?task_id=)."""
    return _ok(_get("/subtasks", {"task_id": task_id}))


@mcp.tool()
def create_subtask(task_id: int, title: str, extra: Optional[dict] = None) -> str:
    """Add a subtask to a task."""
    return _ok(_send("POST", "/subtasks", _body({"task_id": task_id, "title": title}, extra)))


@mcp.tool()
def update_subtask(subtask_id: int, fields: dict) -> str:
    """Update a subtask (PATCH /subtasks/{id}), e.g. {"is_completed": true} or {"title": "..."}."""
    return _ok(_send("PATCH", f"/subtasks/{subtask_id}", fields))


@mcp.tool()
def delete_subtask(subtask_id: int) -> str:
    """Delete a subtask. No undo."""
    return _ok(_send("DELETE", f"/subtasks/{subtask_id}", {}))


@mcp.tool()
def list_checklist(client_id: int) -> str:
    """
    Checklist items on a client record (GET /checklist?client_id=).
    Checklists belong to /clients, not tasks -- ?task_id= returns 400
    "client_id required" (live-tested 2026-10-29).
    """
    return _ok(_get("/checklist", {"client_id": client_id}))


@mcp.tool()
def create_checklist_item(client_id: int, title: str, extra: Optional[dict] = None) -> str:
    """Add a checklist item to a client record (POST /checklist with client_id + title)."""
    return _ok(_send("POST", "/checklist", _body({"client_id": client_id, "title": title}, extra)))


@mcp.tool()
def update_checklist_item(item_id: int, fields: dict) -> str:
    """Update/check a checklist item (PATCH /checklist/{id}), e.g. {"is_completed": true}."""
    return _ok(_send("PATCH", f"/checklist/{item_id}", fields))


@mcp.tool()
def delete_checklist_item(item_id: int) -> str:
    """Delete a checklist item. No undo."""
    return _ok(_send("DELETE", f"/checklist/{item_id}", {}))


# --- Client status reports -------------------------------------------------

@mcp.tool()
def list_status_reports() -> str:
    """Client status reports (GET /reports/status)."""
    return _ok(_get("/reports/status"))


@mcp.tool()
def generate_status_report(client_id: int, extra: Optional[dict] = None) -> str:
    """Generate a client status report (POST /reports/status/generate). Not published to the client until status_report_action(report_id, "publish")."""
    return _ok(_send("POST", "/reports/status/generate", _body({"client_id": client_id}, extra)))


@mcp.tool()
def update_status_report(report_id: int, fields: dict) -> str:
    """Update a status report (PATCH /reports/status/{id})."""
    return _ok(_send("PATCH", f"/reports/status/{report_id}", fields))


@mcp.tool()
def status_report_action(report_id: int, action: str) -> str:
    """
    Run a lifecycle action on a status report: "publish" (sends it to the
    client's portal -- client-facing, confirm with the advisor first),
    "retract", "regenerate", or "delete".
    """
    if action == "delete":
        return _ok(_send("DELETE", f"/reports/status/{report_id}", {}))
    if action not in ("publish", "retract", "regenerate"):
        raise ValueError("action must be publish, retract, regenerate or delete")
    return _ok(_send("POST", f"/reports/status/{report_id}/{action}", {}))


# --- Analytics & team (read-only) -------------------------------------------

_ANALYTICS = {
    "stats": "/stats", "revenue": "/revenue", "activity": "/activity",
    "financials": "/financials", "health_score": "/health-score",
}


@mcp.tool()
def get_analytics(kind: str, params: Optional[dict] = None) -> str:
    """
    Read a OneConnect dashboard: kind is stats, revenue, activity,
    financials or health_score. For one record's activity use
    get_entity_activity.
    """
    if kind not in _ANALYTICS:
        raise ValueError(f"kind must be one of {sorted(_ANALYTICS)}")
    return _ok(_get(_ANALYTICS[kind], params))


@mcp.tool()
def get_entity_activity(params: dict) -> str:
    """Activity for one record (GET /activity/entity), e.g. {"entity_type": "client", "entity_id": 24583}."""
    return _ok(_get("/activity/entity", params))


@mcp.tool()
def list_users(query: Optional[str] = None) -> str:
    """
    The OneConnect user directory (ids, names, emails, roles, office) --
    needed for task assignees, rep reassignment and sharing. `query`
    filters by name/email substring locally.
    """
    users = _get("/users")
    if isinstance(users, dict):
        users, _, _ = _split(users)
    keep = ("id", "name", "email", "role", "tenant_role", "office_id", "is_active")
    out = [{k: u.get(k) for k in keep} for u in users]
    if query:
        q = query.lower()
        out = [u for u in out if q in (u.get("name") or "").lower() or q in (u.get("email") or "").lower()]
    return _ok({"count": len(out), "users": out})


@mcp.tool()
def list_offices() -> str:
    """The office directory (Indy, Evansville, Louisville, Florida, ...)."""
    return _ok(_get("/offices"))


# --- Files: OneConnect's documented presign/finalize upload ------------------

@mcp.tool()
def get_file_download_url(file_id: int) -> str:
    """Short-lived download URL for a scanned file (GET /files/{id}/download-url)."""
    return _ok(_get(f"/files/{file_id}/download-url"))


def _find(d, *keys):
    if isinstance(d, dict):
        for k in keys:
            if d.get(k):
                return d[k]
        for v in d.values():
            hit = _find(v, *keys)
            if hit:
                return hit
    return None


@mcp.tool()
def upload_file_scanned(
    filename: str,
    content_base64: str,
    mime_type: str = "text/plain",
    client_id: Optional[int] = None,
    extra: Optional[dict] = None,
) -> str:
    """
    Upload via OneConnect's documented flow: presign -> PUT to storage ->
    finalize (which triggers the virus scan, so the file leaves
    pending_upload). Prefer this over upload_file once verified. Returns
    each step's raw response so a field-name mismatch is visible.
    """
    raw = base64.b64decode(content_base64)
    steps = {}
    pre = _send("POST", "/files/presign-upload", _body({
        "filename": filename, "name": filename, "mime_type": mime_type,
        "content_type": mime_type, "size_bytes": len(raw), "size": len(raw),
        "client_id": client_id,
    }, extra))
    steps["presign"] = pre
    url = _find(pre, "upload_url", "uploadUrl", "presigned_url", "url", "signedUrl")
    file_id = _find(pre, "file_id", "fileId", "id")
    if not url or not file_id:
        return _ok({"error": "presign response missing upload url or file id", "steps": steps})
    with httpx.Client(timeout=120) as client:
        put = client.put(url, content=raw, headers={"Content-Type": mime_type})
    steps["put"] = {"status": put.status_code, "body": put.text[:500]}
    if put.status_code >= 400:
        return _ok({"error": "storage PUT failed", "steps": steps})
    steps["finalize"] = _send("POST", f"/files/{file_id}/finalize", {})
    return _ok({"file_id": file_id, "steps": steps})


# ---------------------------------------------------------------------------
# Files -- the one place write access is confirmed working. Upload requires
# multipart/form-data; a plain JSON POST 500s.
# ---------------------------------------------------------------------------

@mcp.tool()
def get_file(file_id: int) -> str:
    """
    Fetch a single OneConnect file record by its numeric id, including a
    short-lived signed download_url. GET /files (the list endpoint) has
    historically 500'd -- try list_files first, fall back to this when
    you already know the id.
    """
    return _ok(_get(f"/files/{file_id}"))


@mcp.tool()
def list_files(params: Optional[dict] = None) -> str:
    """
    List File Cabinet records. Pass a filter: {"client_id": 24583} works
    (2026-10-29); the unfiltered list still 500s on OneConnect's side.
    """
    return _list_tool("/files", params, all_pages=False)


@mcp.tool()
def upload_file(
    filename: str,
    content_base64: str,
    mime_type: Optional[str] = None,
    client_id: Optional[int] = None,
    account_id: Optional[str] = None,
    meeting_id: Optional[int] = None,
) -> str:
    """
    Upload a file to OneConnect's File Cabinet (multipart/form-data).

    Allowed types (live-tested): text/plain (.txt) and application/pdf.
    .md / text/markdown is REJECTED with 400 -- save Markdown as .txt.
    content_base64 must pass through the model, so keep each file under
    ~13.5 KB and split larger documents into parts.

    client_id / account_id / meeting_id are sent as form fields to link
    the file to a record. client_id WORKS (verified 2026-10-29 -- the file
    is stored under clients/N/ and shows with list_files(client_id=N));
    account_id is ignored. `_link_check` reports what stuck. Still put the
    client name in the filename and body.
    """
    raw = base64.b64decode(content_base64)
    files = {"file": (filename, raw, mime_type or "application/octet-stream")}
    links = _clean({"client_id": client_id, "account_id": account_id, "meeting_id": meeting_id})
    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{OC_BASE}/files", headers=_headers(), files=files,
            data={k: str(v) for k, v in links.items()},
        )
        _raise_with_body(r)
        rec = r.json()
    if links and isinstance(rec, dict):
        rec["_link_check"] = {
            k: ("linked" if str(rec.get(k)) == str(v) else f"NOT linked (OneConnect returned {rec.get(k)!r})")
            for k, v in links.items()
        }
    return _ok(rec)


@mcp.tool()
def delete_file(file_id: int) -> str:
    """
    Permanently delete a OneConnect file record by its numeric id.
    Confirmed working (re-verified 2026-10-29). No trash/undo -- double
    check the id before calling this on anything that wasn't a test
    upload.
    """
    with httpx.Client(timeout=30) as client:
        r = client.delete(f"{OC_BASE}/files/{file_id}", headers=_headers())
        _raise_with_body(r)
        try:
            return _ok(r.json())
        except Exception:
            return _ok({"status": "deleted", "file_id": file_id})


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
                "real client/prospect data and can now upload/delete files, and "
                "running unauthenticated would leave it open to anyone with the "
                "URL. Set MCP_AUTH_TOKEN and redeploy."
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
