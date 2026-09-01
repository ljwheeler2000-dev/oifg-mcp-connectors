# exchange-mcp

MCP connector for a Smarsh-hosted Microsoft Exchange mailbox (EWS) — email, calendar, and contacts. 30 tools covering read/search/send/reply/forward/draft, calendar CRUD, contact CRUD, and folder management.

## Requirements

- A Smarsh-hosted Exchange mailbox (username + password — this uses EWS Basic Auth, not OAuth)
- A Railway account (or any host that can run a small Python container)

## What this can't do

`exchange_create_calendar_event` will throw a type error if you pass attendees at creation time — create the event first with no attendees, then call `exchange_add_attendees` (event_id, attendees[]) right after. That two-step flow is fully working; there's no need to have anyone forward an invite manually. Beyond that, this connector covers full calendar/contact CRUD and the full email lifecycle (read, search, send, reply, forward, draft, folder/category/flag management) — there's no other known gap in what it exposes versus what EWS supports.

## Deploy

1. Fork or clone this repo, deploy to Railway with **Root Directory** set to `exchange-mcp`.
2. Copy `.env.example` to a Railway service variable set and fill it in with your own EWS credentials. Generate your own `MCP_AUTH_TOKEN` — a random secret, e.g.:
   ```
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
3. Deploy. Railway assigns a public URL and injects `PORT` automatically — the server detects this and serves Streamable HTTP with bearer-token auth instead of stdio.
4. Add to your Claude Desktop config:
   ```json
   "exchange": {
     "type": "http",
     "url": "https://<your-service>.up.railway.app",
     "headers": { "Authorization": "Bearer <your MCP_AUTH_TOKEN>" }
   }
   ```
   If your Claude Desktop build doesn't support the `"type": "http"` remote format natively, use the `mcp-remote` bridge instead — see the root README.
5. Restart Claude Desktop.

## Known risk to test first

EWS Basic Auth from a new IP/datacenter can trip some mail providers' security posture. Test your deployment live before relying on it — if login fails where it worked locally, check with whoever administers your Exchange/Smarsh account about IP allowlisting.

## Local dev

Run `python server.py` with no `PORT` set in your environment and it falls back to stdio, same as before this repo existed.
