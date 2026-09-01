# zoom-mcp

MCP connector for Zoom Cloud Recordings and AI Companion meeting summaries — list recordings, pull transcripts, track which meetings have already been processed.

## Requirements

- A Zoom Server-to-Server OAuth app (Zoom App Marketplace -> Build App -> Server-to-Server OAuth) with cloud recording scopes (and `meeting_summary:read:admin` if you want the meeting-summary tools — note this scope's reliability specifically on Server-to-Server apps is unconfirmed; test it live rather than assuming)
- A Railway account

## What this can't do

`get_meeting_summary` only ever returns Zoom's AI-generated abstract (key takeaways/action items) — never a verbatim transcript. That AI summary feature also has to be turned on at the account level ("Meeting summary with AI" in Zoom's admin settings) before any summary exists to fetch, and it's off by default. Separately, Zoom's auto-record doesn't reliably trigger for real client meetings, so a recording this connector could pull may simply not exist unless someone started it manually; and two meetings held back-to-back in the same Personal Meeting Room can merge into a single continuous recording with no automatic split between them. In practice, treat this connector as a fallback transcript source rather than the primary one — a live meeting-notes capture tends to be more complete.

## Deploy

1. Deploy this repo to Railway with **Root Directory** set to `zoom-mcp`.
2. Fill in `.env.example` with your Zoom app credentials and your own `MCP_AUTH_TOKEN`.
3. Decide on a state backend (see below) and set `STATE_BACKEND` accordingly. The default (`file`) needs no extra setup.
4. Deploy, then add to Claude Desktop the same way as the other connectors (see root README) — swap in this service's URL and token.

## State backend

This server tracks which meetings have already been pulled into a workflow, so repeat syncs don't redo work. Two options:

- **`file` (default)** — a local JSON file. Works immediately, no setup. Without a Railway volume attached it's ephemeral (wiped on redeploy) — the only consequence is a meeting or two might get reprocessed after a redeploy, not a correctness problem. To make it persistent, attach a small Railway volume and point `STATE_PATH` at it.
- **`supabase`** — point at your own Supabase project instead (`SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_TABLE`). Useful if you already run other infrastructure on Supabase and want one place for durable state. Table schema:
  ```sql
  create table zoom_processed_meetings (
    meeting_uuid text primary key,
    processed_at timestamptz,
    note text
  );
  ```

## Local dev

Run `python server.py` with no `PORT` set and it falls back to stdio.
