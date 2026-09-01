# advisor-evolution-mcp

MCP connector for Advisor Evolution's (app.advisorevolution.io) Workspace API — pipeline/relationships, business snapshot, training, and released coaching.

## Requirements

- An Advisor Evolution account with a "Connect my AI" token (AE Settings -> Connected Apps). This is scoped and revocable per-advisor — nothing shared between deployers.
- A Railway account

## What this can't do

There's no hard delete for a relationship — only `archive_relationship`, which is reversible. `create_relationship`'s joint-work-partner field needs a real id from AE's own partner directory; if the advisor's org has never set up joint-work partners inside AE itself, that directory can be empty and there's nothing to select. `team:read`/`org:read` scopes are deliberately withheld from advisor-level tokens by AE's own design (not a bug to report) — this connector only ever sees the connected advisor's own data, never a team- or office-wide view.

## Deploy

1. Deploy this repo to Railway with **Root Directory** set to `advisor-evolution-mcp`.
2. Fill in `.env.example` with your own `ADVISOR_EVOLUTION_TOKEN` and your own `MCP_AUTH_TOKEN`.
3. Deploy, then add to Claude Desktop the same way as the other connectors (see root README).

This connector is fully stateless — no local files, no database. It's a thin wrapper over AE's REST API.

## Local dev

Run `python server.py` with no `PORT` set and it falls back to stdio.
