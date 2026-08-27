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
   Replace `<connector-name>` with whatever you want it labeled (e.g. `exchange`, `zoom`, `advisor-evolution`), and repeat the block per connector you deploy — they all merge under the same `mcpServers` key.

   If your Claude Desktop build doesn't support the `"type": "http"` remote format natively, use the [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) bridge instead — point it at the same URL and token via its `--header` flag, and point Claude Desktop's config at the `mcp-remote` command rather than the URL directly. See that package's README for the exact `command`/`args` shape.
4. Restart Claude Desktop.

Nobody's credentials are shared between deployments — every deployer supplies their own secrets. This repo contains no secrets of any kind.

## License

MIT — see [LICENSE](./LICENSE).
