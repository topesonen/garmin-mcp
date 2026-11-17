# garmin-mcp

An MCP server that lets Claude read my Garmin training and health data, so I can talk about my training in the Claude app or on claude.ai without pasting numbers into the chat. Claude reads the table descriptions, writes its own SQL and runs it through this server, and for detail the warehouse does not hold it calls the Garmin Connect Personal API live.

The warehouse comes from [garmin-llm](https://github.com/topesonen/garmin-llm), which syncs my activities, and one row per day of wellness, sleep and HRV, from the Garmin Connect API into a DuckDB database every hour. garmin-llm is about text-to-SQL with small models and an eval harness; this repo is the other direction: a capable model with direct, read-only access to the same table.

## What MCP is, briefly

The [Model Context Protocol](https://modelcontextprotocol.io) is a standard way to give a model tools. A server describes its tools (name, description, input schema), and a client such as Claude Code or claude.ai lists them, lets the model call them and passes the results back. The same server works with any client that speaks the protocol.

## Tools

### Warehouse

| Tool | What it returns |
|---|---|
| `get_schema` | Columns and types of the `activities` and `daily_summaries` tables, read from the database itself, with row counts and date ranges |
| `get_notes` | garmin-llm's [`docs/schema.md`](https://github.com/topesonen/garmin-llm/blob/main/docs/schema.md) and [`docs/schema_daily.md`](https://github.com/topesonen/garmin-llm/blob/main/docs/schema_daily.md): units and meaning of each column, and tips for writing queries |
| `run_query` | The result of one SQL query: column names, up to 200 rows (at most 2000), and whether rows were cut |

`activities` has one row per workout. `daily_summaries` has one row per day: steps, calories, resting HR, stress, Body Battery, respiration and SpO2, plus the night's sleep (stages, score, sleep need) and HRV (nightly average, baseline, status).

All three are marked read-only. Queries run on a DuckDB connection opened `read_only=True` with external access disabled, so a query cannot change the database or read other files or the network. A query is stopped after 10 seconds. Database errors go back to the model as tool errors, so it can fix its query.

### Live, from the Garmin Connect Personal API

[`garmin_live.py`](garmin_live.py) has one tool per endpoint of Garmin's [Personal API](https://developer.garmin.com/gc-developer-program/personal/swagger-docs/) (all 25 that exist as of 2026-11-17), for detail the warehouse does not hold:

| Area | Tools |
|---|---|
| Activities | `search_activities`, `get_activity` (summary and splits), `get_activity_workouts`, `get_activity_fit` |
| Sleep, HRV, heart rate | `get_sleep`, `get_sleep_range`, `get_hrv`, `get_hrv_range`, `get_daily_heart_rate`, `get_spo2` |
| Day summaries | `get_daily_summary`, `get_daily_summaries`, `get_body_battery`, `get_stress_range`, `get_steps_range`, `get_hydration_range`, `get_wellness_activities` |
| Profile and body | `get_heart_rate_zones`, `get_user_settings`, `get_fitness_age`, `get_weight_entries`, `get_weight_summary`, `get_weight_goals` |
| Logged by me | `get_nutrition`, `get_menstrual_cycle_summary`, `get_menstrual_cycle_day` |

- They return Garmin's JSON as it is, coordinates and names included (my decision, 2026-11-17). `get_activity_fit` is the exception: it downloads the original FIT file, parses it with [fitdecode](https://github.com/polyvertex/fitdecode), and returns the session, every lap and the record sampled every 15 s (at most 1500 samples), with all GPS fields removed.
- Marked read-only and open-world (they reach outside the server).
- Date arguments are checked, and ranges are kept to Garmin's documented maximums (31 days for sleep, 28 for steps and stress, and so on). Replies over 200 kB are refused with a hint to narrow the range or use the warehouse.
- The API key is exchanged for a 24-hour token, which the server keeps in memory. Garmin rate limits the key, the token endpoint included (HTTP 429 after about 100 calls in half an hour when I tested), so the instructions tell Claude to prefer the warehouse.

Things Garmin's documentation does not say: the activity download is a zip holding the FIT file; `get_body_battery`'s `chunks` is a count of intervals per day, and Garmin answers 404 to a non-number; the range limits are not enforced.

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
| `GARMIN_DAILY_DOC` | both | Path to garmin-llm's `docs/schema_daily.md` (default next to `GARMIN_SCHEMA_DOC`) |
| `GARMIN_API_KEY` | both | Garmin Personal API key for the live tools; without it they answer with an error and the warehouse tools still work. Falls back to `GARMIN_KEY_FILE` (default `~/.config/garmin-llm/garmin.env`) |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | HTTP | A GitHub OAuth app whose callback is `<PUBLIC_URL>/github/callback` |
| `GITHUB_ALLOWED_USER` | HTTP | The only GitHub login accepted |
| `PUBLIC_URL` | HTTP | Base URL the server is reached at (default `http://127.0.0.1:8765`) |
| `TOKEN_DB` | HTTP | SQLite file for clients and tokens (default `tokens.sqlite`) |

To try the HTTP server locally, run it and connect the [MCP Inspector](https://github.com/modelcontextprotocol/inspector) (`npx @modelcontextprotocol/inspector`) to `http://127.0.0.1:8765/mcp` with Streamable HTTP. It goes through the same GitHub login.

## Deployment

On my server the HTTP server runs as a systemd service ([`deploy/garmin-mcp.service`](deploy/garmin-mcp.service)): its own system user, code owned by root, the token database in `/var/lib/garmin-mcp`, listening on `127.0.0.1:8765` only. Tailscale Funnel publishes it on the internet over HTTPS; nothing else on the machine is public. Settings are in a root-only environment file; the Garmin key comes from the sync's root-only key file, so a new key is written in one place.

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
| Live tools for all 25 Personal API endpoints, each called once against my account | Done (2026-11-17) |
| Refusing a second GitHub account, checked for real | To do |
| Client ID metadata documents (CIMD), claude.ai's recommended client option | Not supported; the connector uses dynamic registration |

Built with the [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) 2.3.0, DuckDB 1.5.6 and fitdecode 0.11.0.
