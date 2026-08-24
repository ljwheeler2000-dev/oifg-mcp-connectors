# advisor-evolution-mcp

MCP connector for Advisor Evolution's (app.advisorevolution.io) Workspace API — pipeline/relationships, business snapshot, training, and released coaching.

## Requirements

- An Advisor Evolution account with a "Connect my AI" token (AE Settings -> Connected Apps). This is scoped and revocable per-advisor — nothing shared between deployers.
- A Railway account

## Deploy

1. Deploy this repo to Railway with **Root Directory** set to `advisor-evolution-mcp`.
2. Fill in `.env.example` with your own `ADVISOR_EVOLUTION_TOKEN` and your own `MCP_AUTH_TOKEN`.
3. Deploy, then add to Claude Desktop the same way as the other connectors (see root README).

This connector is fully stateless — no local files, no database. It's a thin wrapper over AE's REST API.

## Local dev

Run `python server.py` with no `PORT` set and it falls back to stdio.
