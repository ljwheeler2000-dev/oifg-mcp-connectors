# oneconnect-mcp

MCP connector for OneConnect.pro (the firm's CRM, built by HoosAI) — read-only access to accounts, prospects, tasks, meetings, and contacts.

## Requirements

- A OneConnect.pro account with an API token (Settings -> API Tokens). Scope it to at least `accounts:read`, `prospects:read`, `tasks:read`, `meetings:read`.
- Your OneConnect tenant/office slug (visible in your OneConnect URL, or ask your admin).
- A Railway account.

## Why read-only

OneConnect's API doesn't expose write scopes for accounts/prospects yet — there's no `accounts:write` or `prospects:write` to request, on any token, as of this writing. This connector only ever calls `GET`. If a skill needs to "add a note to a client," it should draft that note as text for you to paste into OneConnect by hand — not pretend to write it via this connector. Track the vendor's progress on this via ticket OC-150 if you're at OIFG; re-check `check_connection()`'s output periodically since OneConnect's endpoint availability has changed between checks before.

## Deploy

1. Deploy this repo to Railway with **Root Directory** set to `oneconnect-mcp`.
2. Fill in `.env.example` with your own `ONECONNECT_TOKEN`, your own `ONECONNECT_TENANT_SLUG`, and your own `MCP_AUTH_TOKEN`.
3. Deploy, then add to your AI assistant's MCP config the same way as the other connectors (see root README).
4. Call `check_connection()` first — it reports which endpoints are actually live right now rather than assuming a fixed list.

This connector is fully stateless — no local files, no database. It's a thin wrapper over OneConnect's REST API.

## Local dev

Run `python server.py` with no `PORT` set and it falls back to stdio.
