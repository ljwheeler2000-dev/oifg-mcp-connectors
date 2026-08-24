# OIFG MCP Connectors

MCP (Model Context Protocol) connectors for financial advisor tooling, built for use with Claude Desktop / Cowork. Each connector is a standalone service deployable to Railway (or any host that can run a Python container).

## Connectors

| Folder | Wraps | Auth |
|---|---|---|
| [`exchange-mcp`](./exchange-mcp) | Smarsh-hosted Microsoft Exchange (EWS) — email, calendar, contacts | EWS username/password |
| [`zoom-mcp`](./zoom-mcp) | Zoom Cloud Recordings + AI Companion meeting summaries | Zoom Server-to-Server OAuth |
| [`advisor-evolution-mcp`](./advisor-evolution-mcp) | Advisor Evolution (app.advisorevolution.io) Workspace API — pipeline, business, training, coaching | Static bearer token from AE's "Connect my AI" |

## Deploying your own copy

Each connector folder is independently deployable. See that folder's own README for its required environment variables. In short, for each connector:

1. Deploy the folder as its own Railway service (set the service's **Root Directory** to the connector's folder).
2. Set the connector's required env vars (credentials for the underlying service, plus `MCP_AUTH_TOKEN` — a random secret you generate yourself).
3. Once deployed, Railway gives the service a public URL. Add it to your Claude Desktop config as a remote MCP server, sending `Authorization: Bearer <your MCP_AUTH_TOKEN>` on every request.
4. Restart Claude Desktop.

Nobody's credentials are shared between deployments — every deployer supplies their own secrets. This repo contains no secrets of any kind.

## License

MIT — see [LICENSE](./LICENSE).
