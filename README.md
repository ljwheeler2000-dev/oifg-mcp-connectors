# OIFG MCP Connectors

MCP (Model Context Protocol) connectors for financial advisor tooling, built for use with Claude Desktop / Cowork. Each connector is a standalone service deployable to Railway (or any host that can run a Python container).

## Connectors

| Folder | Wraps | Auth |
|---|---|---|
| [`exchange-mcp`](./exchange-mcp) | Smarsh-hosted Microsoft Exchange (EWS) — email, calendar, contacts | EWS username/password |
| [`zoom-mcp`](./zoom-mcp) | Zoom Cloud Recordings + AI Companion meeting summaries | Zoom Server-to-Server OAuth |
| [`advisor-evolution-mcp`](./advisor-evolution-mcp) | Advisor Evolution (app.advisorevolution.io) Workspace API — pipeline, business, training, coaching | Static bearer token from AE's "Connect my AI" |
| [`oneconnect-mcp`](./oneconnect-mcp) | OneConnect.pro (firm CRM) — accounts, prospects, tasks, meetings, contacts, clients, files | Static bearer token from OneConnect Settings -> API Tokens |

## Capabilities & limitations

Every connector here is a thin wrapper over its vendor's own API — if the vendor's API can't do something, the connector can't either. This section reflects what's actually been tested live (most recently 2026-09-01), not a feature aspiration. Each connector's own README has the full detail; this is the summary.

### exchange-mcp (email / calendar / contacts)
**Can:** read/search/send/reply/forward/draft email; full calendar CRUD; full contact CRUD; add attendees to an *existing* event via `exchange_add_attendees`.
**Cannot:** add attendees in the same call that creates an event — `exchange_create_calendar_event` throws a type error if attendees are passed at creation. Create the event first, then call `exchange_add_attendees` immediately after.
**Known limitation:** wraps EWS Basic Auth (username/password) against a Smarsh-hosted mailbox specifically — not OAuth, not a generic Exchange/Office 365 setup. A login from a new IP/datacenter can occasionally trip a mail provider's security posture; test live after deploy.

### zoom-mcp (recordings / transcripts / summaries)
**Can:** list recent cloud recordings, pull transcripts, mark/unmark recordings as processed, list and fetch AI Companion meeting summaries.
**Cannot:** return a verbatim transcript from `get_meeting_summary` — it only ever returns Zoom's AI-generated abstract (key takeaways/action items), never transcript text.
**Known limitations:** meeting summaries require the account-level "Meeting summary with AI" setting, off by default and admin-controlled; Zoom's auto-record doesn't reliably trigger for real client meetings, so a recording may simply not exist unless started manually; back-to-back meetings sharing one Personal Meeting Room merge into a single continuous recording with no automatic splitting. In practice this API is a fallback transcript source, not the primary one.

### advisor-evolution-mcp (pipeline / training / coaching)
**Can:** read and create/update pipeline relationships, advance a relationship's stage, list joint-work partners, read business snapshot/training/coaching status, mark training assignments complete.
**Cannot:** permanently delete a relationship — only a reversible `archive_relationship` exists.
**Known limitations:** `create_relationship`'s joint-work-partner field needs a real id from AE's own partner directory, which can be empty for an org that's never used AE's joint-work feature; `team:read`/`org:read` are deliberately withheld from advisor-level tokens by AE's own design — this connector only ever sees the connected advisor's own data.

### oneconnect-mcp (firm CRM)
**Can (read):** list accounts, prospects, tasks, meetings, contacts, and clients.
**Can (write, verified 2026-09-01):** upload a file to the general File Cabinet, fetch a single file record by id, permanently delete a file by id. Uploads must be multipart form data — a JSON POST to the same endpoint returns a 500.
**Cannot:** create or update accounts, prospects, tasks, meetings, or clients. `accounts:write`/`prospects:write` don't exist as scopes on any OneConnect token as of this testing; `tasks:write`/`clients:write`/`meetings:write` are real, grantable scopes, but every create attempt against them returns a 500 on OneConnect's side (reported to the vendor).
**Known limitations:** `GET /files` (the list endpoint) 500s — no way to browse existing files, only fetch one by known id; an uploaded file can't be linked to a specific client/account/meeting through this API; file deletion is permanent, no trash/undo; some OneConnect endpoints are tenant-scoped in the URL and others aren't, which is how OneConnect built it. OneConnect's own support tracker has marked at least one ticket (OC-176) "Fixed" that live re-testing shows is not actually resolved — don't take a vendor "Fixed" label at face value.

## Deploying your own copy

Each connector folder is independently deployable. See that folder's own README for its required environment variables. In short, for each connector:

1. Deploy the folder as its own Railway service (set the service's **Root Directory** to the connector's folder).
2. Set the connector's required env vars (credentials for the underlying service, plus `MCP_AUTH_TOKEN` — a random secret you generate yourself, e.g. `python -c "import secrets; print(secrets.token_urlsafe(32))"`). **`MCP_AUTH_TOKEN` is required, not optional** — every connector in this repo refuses to start a remote deployment without one, since each wraps real credentials/data and running unauthenticated would leave it open to anyone with the URL.
3. Once deployed, Railway gives the service a public URL. Add it to your Claude Desktop config as a remote MCP server:
   ```json
   {
     "mcpServers": {
       "<connector-name>": {
         "type": "http",
         "url": "https://<your-service>.up.railway.app",
         "headers": { "Authorization": "Bearer <your MCP_AUTH_TOKEN>" }
       }
     }
   }
   ```
   Replace `<connector-name>` with whatever you want it labeled (e.g. `exchange`, `zoom`, `advisor-evolution`, `oneconnect`), and repeat the block per connector you deploy — they all merge under the same `mcpServers` key.

   If your Claude Desktop build doesn't support the `"type": "http"` remote format natively, use the [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge instead — point it at the same URL and token via its `--header` flag, and point Claude Desktop's config at the `mcp-remote` command rather than the URL directly. See that package's README for the exact `command`/`args` shape.
4. Restart Claude Desktop.

**Using Claude's Settings → Connectors UI instead of a config file?** Set Authentication to **None**, not the auto-detected "Always required" option — none of these connectors implement OAuth, they just return a 401 on a missing bearer token, and Claude's dialog misreads that as an OAuth-protected resource. Supply the token via the dialog's "Additional request headers" section instead: header name `Authorization`, value `Bearer <your MCP_AUTH_TOKEN>` (the literal word "Bearer" plus a space).

If your AI assistant isn't Claude, most MCP-capable clients accept the same `"type": "http"` + bearer-header remote-server shape — check your client's docs for where that config lives. If your assistant can't speak MCP at all, these connectors still work as a plain REST API — point whatever integration your assistant supports at the same Railway URL with the same bearer token.

Nobody's credentials are shared between deployments — every deployer supplies their own secrets. This repo contains no secrets of any kind.

## License

MIT — see [LICENSE](./LICENSE).
