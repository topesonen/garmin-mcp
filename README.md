# garmin-mcp

An MCP server that lets Claude read my Garmin training data, so I can talk about my training in the Claude app or on claude.ai without pasting numbers into the chat. Claude reads the table description, writes its own SQL and runs it through this server.

The data comes from [garmin-llm](https://github.com/topesonen/garmin-llm), which syncs my activities from the Garmin Connect API into a DuckDB database every hour. garmin-llm is about text-to-SQL with small models and an eval harness; this repo is the other direction: a capable model with direct, read-only access to the same table.

## What MCP is, briefly

The [Model Context Protocol](https://modelcontextprotocol.io) is a standard way to give a model tools. A server describes its tools (name, description, input schema), and a client such as Claude Code or claude.ai lists them, lets the model call them and passes the results back. The same server works with any client that speaks the protocol.

## Tools

| Tool | What it returns |
|---|---|
| `get_schema` | Columns and types of the `activities` table, read from the database itself, plus the row count and the date range |
| `get_notes` | garmin-llm's [`docs/schema.md`](https://github.com/topesonen/garmin-llm/blob/main/docs/schema.md): units and meaning of each column, and tips for writing queries |
| `run_query` | The result of one SQL query: column names, up to 200 rows (at most 2000), and whether rows were cut |

All three are marked read-only. Queries run on a DuckDB connection opened `read_only=True` with external access disabled, so a query cannot change the database or read other files or the network. A query is stopped after 10 seconds. Database errors go back to the model as tool errors, so it can fix its query.

## Two ways to run it

**stdio, for Claude Code on the same machine.** No network, no auth:

```
claude mcp add garmin -- /path/to/.venv/bin/python /path/to/server.py
```

**Streamable HTTP with OAuth, for claude.ai and the Claude apps.** claude.ai calls connectors from Anthropic's cloud, so the server has to be reachable over HTTPS from the internet, and it needs real authentication:

```
python server.py --http        # 127.0.0.1:8765, endpoint /mcp
```

The HTTP server is stateless and answers with plain JSON: each tool call is one request and one response.

## How the login works

The MCP Python SDK serves the OAuth endpoints and checks PKCE and bearer tokens. [`auth.py`](auth.py) is the storage and policy behind them, with the login handed over to GitHub:

```
claude.ai                    garmin-mcp                       GitHub
   |  POST /mcp (no token)       |                               |
   |---------------------------->|                               |
   |  401, resource metadata     |                               |
   |<----------------------------|                               |
   |  read metadata, /register   |                               |
   |---------------------------->|                               |
   |  /authorize (browser)       |                               |
   |---------------------------->|  redirect to GitHub login     |
   |                             |------------------------------>|
   |                             |  /github/callback?code=...    |
   |                             |<------------------------------|
   |                             |  who is this? (only my login) |
   |                             |------------------------------>|
   |  redirect with one-time code|                               |
   |<----------------------------|                               |
   |  /token (code + PKCE)       |                               |
   |---------------------------->|                               |
   |  access + refresh token     |                               |
   |<----------------------------|                               |
   |  POST /mcp (Bearer token)   |                               |
   |---------------------------->|                               |
```

- Only one GitHub account is accepted (`GITHUB_ALLOWED_USER`). Any other login is sent back with `access_denied`.
- A client can register only with claude.ai's callback URL as its redirect, or a localhost URL while the server itself runs on localhost. Nobody can register a client that would receive codes from my login elsewhere.
- Access tokens last an hour and refresh tokens 30 days; a refresh replaces the refresh token, and revoking either token ends the login.
- Codes and tokens are stored in SQLite as SHA-256 hashes, so a copy of the file holds no working tokens.
- The GitHub token is used once, to read the login name, and is not kept.

## Setup

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Settings come from the environment, or from a `.env` file next to `auth.py` (ignored by git):

| Variable | Used by | Meaning |
|---|---|---|
| `GARMIN_DB` | both | Path to `garmin.duckdb` (default `../garmin-llm/data/warehouse/garmin.duckdb`) |
| `GARMIN_SCHEMA_DOC` | both | Path to garmin-llm's `docs/schema.md` (default `../garmin-llm/docs/schema.md`) |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | HTTP | A GitHub OAuth app whose callback is `<PUBLIC_URL>/github/callback` |
| `GITHUB_ALLOWED_USER` | HTTP | The only GitHub login accepted |
| `PUBLIC_URL` | HTTP | Base URL the server is reached at (default `http://127.0.0.1:8765`) |
| `TOKEN_DB` | HTTP | SQLite file for clients and tokens (default `tokens.sqlite`) |

To try the HTTP server locally, run it and connect the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) (`npx @modelcontextprotocol/inspector`) to `http://127.0.0.1:8765/mcp` with Streamable HTTP. It goes through the same GitHub login.

## Deployment

On my server the HTTP server runs as a systemd service ([`deploy/garmin-mcp.service`](deploy/garmin-mcp.service)): its own system user, code owned by root, the token database in `/var/lib/garmin-mcp`, listening on `127.0.0.1:8765` only. Tailscale Funnel publishes it on the internet over HTTPS; nothing else on the machine is public. Settings are in a root-only environment file.

Things I learned on the way:

- claude.ai calls connectors from Anthropic's cloud, also from the phone app, so the server has to be public. A Tailscale-only address cannot work.
- With Funnel on port 8443 the endpoint answered from the internet, but no request from claude.ai ever arrived. On port 443 it connected. Funnel allows only 443, 8443 and 10000, so the server's other page moved off 443.
- Right after Funnel is switched on, the public relays take a minute or two before every one of them answers. A connect attempt in that window fails part way through the OAuth discovery.
- Behind a proxy the `Host` header is the public name. The SDK's DNS rebinding protection refuses unknown hosts with `421`, so the server allows the host from `PUBLIC_URL`.
- In the connector dialog, choose "Register automatically" (dynamic client registration). The recommended "published identity" option needs CIMD support on the server.

## Status

| Part | State |
|---|---|
| Tools over stdio, used from Claude Code | Done |
| Streamable HTTP, checked with the MCP Inspector | Done |
| OAuth with GitHub login, checked end to end with the Inspector | Done |
| Running on my server behind Tailscale Funnel, as a claude.ai custom connector | Done, used from claude.ai on the web and the phone |
| Refusing a second GitHub account, checked for real | To do |
| Client ID metadata documents (CIMD), claude.ai's recommended client option | Not supported; the connector uses dynamic registration |

Built with the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 2.3.0 and DuckDB 1.5.6.
