# oneconnect-mcp

MCP connector for OneConnect.pro (the firm's CRM, built by HoosAI) — read access to accounts, prospects, tasks, meetings, clients, and contacts, plus working file upload/fetch/delete against OneConnect's File Cabinet.

## Requirements

- A OneConnect.pro account with an API token (Settings -> API Tokens). Scope it to at least `accounts:read`, `prospects:read`, `tasks:read`, `meetings:read`, `clients:read`, and `files:read`/`files:write` if you want the file tools.
- Your OneConnect tenant/office slug (visible in your OneConnect URL, or ask your admin).
- A Railway account.

## What actually works (verified 2026-09-01)

This connector only exposes what's actually been tested against a live OneConnect account — not what a token's scope list implies should work. A scope existing on your token doesn't mean OneConnect's backend has actually implemented that path.

**Read (all confirmed working):** `list_accounts`, `list_prospects`, `list_tasks`, `list_meetings`, `list_contacts`, `list_clients`. OneConnect returns 50 records per call; since 2026-10-29 every `list_*` tool follows paging by default (`all_pages=True`) and reports `_paging.complete` so a partial list is never mistaken for the whole book. Each also takes `params` for query-string filters.

**Search (added 2026-10-29):** `find_person(query)` searches accounts, prospects, contacts **and** `/clients` (pipeline) records at once by name, email or phone. A person can exist only as a `/clients` pipeline record (e.g. one opened by the Advisor Evolution sync) with no household or contact — this is how you find them.

**Probe (added 2026-10-29):** `api_get(path)` — read-only GET against any path, for testing scopes this connector has no dedicated tool for (`/reports`, `/{tenant}/planner`, `/team`, …). GET only; it cannot change data.

**Write (confirmed working):** `upload_file`, `get_file`, `delete_file` — OneConnect's general File Cabinet. Uploads must be sent as multipart form data; a JSON POST to the same endpoint returns a 500. A newly uploaded file starts in `pending_upload` status with no way to link it to a specific client/account/meeting through this API. Deletion is permanent — no trash or undo.

**Writes, live re-test 2026-10-29:**
- **Tasks:** `create_task` and `update_task` work. `account_id` is ignored on update. There's no GET by id (405); `delete_record` looks tasks up through the list instead.
- **Clients (pipeline):** `create_client` and `update_client` work, including stage, deal value and next review date. There's no delete (403 `insufficient_scope`); set the stage to `lost` instead.
- **Meetings:** `create_meeting` still 500s (`Failed to create meeting` with a `correlationId`), which is a vendor bug.
- **Files:** `upload_file` with `client_id` links the file (it's stored under `clients/{id}/`); `account_id` is ignored. `list_files` works when filtered (`{"client_id": N}`); unfiltered it still 500s.
- **Accounts / prospects:** still no write scope exists.

**Endpoint catalog:** OneConnect publishes an authenticated catalog at `GET /api/docs` (scopes, paging rules, every path). Read it with `api_get("/docs", max_chars=60000)` or page through it with `body_offset`. Paged endpoints take `?limit=&offset=` and return `X-Has-More`.

**Full catalog tools (added 2026-10-29, writes not yet live-verified):**
- **Contacts:** `create_contact` (link to a client with `client_id` or a household with `account_id`), `update_contact`, and `delete_contact` (name-guarded).
- **Notes:** `list_notes`, `create_note`, `update_note` and `delete_note`; plus `list_next_steps`.
- **Clients:** `get_client`, `move_client_stage`, `archive_client`, `reassign_client_rep`, `list_client_shares`, `share_client`, `unshare_client` and `list_client_portal_users`.
- **Tasks:** `list_subtasks`, `create_subtask`, `update_subtask`, `delete_subtask`, `list_checklist`, `create_checklist_item`, `update_checklist_item` and `delete_checklist_item`.
- **Meetings:** `get_meeting`.
- **Status reports:** `list_status_reports`, `generate_status_report` and `update_status_report`; `status_report_action` (publish/retract/regenerate/delete). **Publish is client-facing.**
- **Analytics and team:** `get_analytics` (stats/revenue/activity/financials/health_score), `get_entity_activity`, `list_users` and `list_offices`.
- **Files:** `get_file_download_url`, and `upload_file_scanned`, which uses OneConnect's presign → PUT → finalize flow and triggers the virus scan.

Request bodies aren't documented by OneConnect. Every write takes the common fields plus `extra` for anything else, and returns the vendor's error body if a field name is wrong. Planner (`/planner/*`) and QA (`/scrum/qa/*`) are 404 on this tenant (modules off), so they have no tools; use `api_get` if they're ever enabled.

**A caution on trusting "Fixed" labels:** OneConnect's own support tracker marked ticket OC-176 (a request to add write scopes for accounts/prospects) "Fixed." Live re-testing on 2026-09-01 shows it isn't — those write paths still don't exist. Don't assume a vendor ticket status reflects reality; re-test with `check_connection()` and a real attempt before relying on anything that was previously reported broken.

**Routing note:** some OneConnect endpoints are tenant-scoped in the URL (`/{tenant}/accounts`, `/{tenant}/prospects`) and others are top-level (`/tasks`, `/meetings`, `/contacts`, `/clients`, `/files`). That's how OneConnect built their API — this connector follows it as-is rather than normalizing it.

Call `check_connection()` first any time — it reports which endpoints are actually live right now, including a `write_support` summary, rather than assuming a fixed list.

## Deploy

1. Deploy this repo to Railway with **Root Directory** set to `oneconnect-mcp`.
2. Fill in `.env.example` with your own `ONECONNECT_TOKEN`, your own `ONECONNECT_TENANT_SLUG`, and your own `MCP_AUTH_TOKEN`.
3. Deploy, then add to your AI assistant's MCP config the same way as the other connectors (see root README).
4. Call `check_connection()` first — it reports which endpoints are actually live right now rather than assuming a fixed list.

This connector is fully stateless — no local files, no database. It's a thin wrapper over OneConnect's REST API.

## Local dev

Run `python server.py` with no `PORT` set and it falls back to stdio.
