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

**Write tools present but vendor-blocked (added 2026-10-29):** `create_task`/`update_task`, `create_meeting`/`update_meeting`, `create_client`/`update_client`, and `delete_record` (guarded by an exact title match, for cleaning up test records). Errors return OneConnect's body plus any trace headers for the support ticket. `upload_file` now also sends `client_id`/`account_id`/`meeting_id` and reports in `_link_check` whether OneConnect honored the link. `list_files` tries the list endpoint and returns the vendor error if it still fails.

**Write (does not work as of the last live test, despite the scope existing on the token):** creating/updating accounts, prospects, tasks, meetings, or clients. `accounts:write` and `prospects:write` aren't real scopes on any OneConnect token as of this testing — there's nothing to request. `tasks:write`, `clients:write`, and `meetings:write` are real, grantable scopes, but every create call against those returns a 500 on OneConnect's side — a vendor bug, reported to HoosAI, not something this connector can work around.

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
