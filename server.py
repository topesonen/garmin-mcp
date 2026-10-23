"""MCP server for the Garmin training warehouse built by garmin-llm.

Gives Claude three read-only tools: the live table schema, the schema notes
(units, meanings, query tips) and a SQL query with a row limit and a timeout.

Settings, from the environment:
    GARMIN_DB          path to garmin.duckdb (default ../garmin-llm/data/warehouse/garmin.duckdb)
    GARMIN_SCHEMA_DOC  path to garmin-llm's docs/schema.md (default ../garmin-llm/docs/schema.md)

Usage:
    python server.py                    stdio, for Claude Code:
                                        claude mcp add garmin -- /path/to/.venv/bin/python /path/to/server.py
    python server.py --http [--port N]  Streamable HTTP on 127.0.0.1, endpoint /mcp
"""

import argparse
import datetime
import decimal
import os
import threading
from pathlib import Path

import duckdb
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("GARMIN_DB", HERE.parent / "garmin-llm/data/warehouse/garmin.duckdb"))
SCHEMA_DOC = Path(os.environ.get("GARMIN_SCHEMA_DOC", HERE.parent / "garmin-llm/docs/schema.md"))
TABLE = "activities"
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000
TIMEOUT_S = 10

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

server = MCPServer(
    name="garmin",
    instructions=(
        "Training data from Garmin Connect, one row per activity in the `activities` table. "
        "Call get_notes once before writing queries: it gives units and meanings "
        "(pace is in seconds per km, durations in seconds). Query only `activities`."
    ),
)


def connect():
    # Read-only, and no access to other files or the network, so a query
    # can only read the warehouse.
    return duckdb.connect(str(DB_PATH), read_only=True, config={"enable_external_access": False})


def to_json_value(value):
    """DuckDB returns datetimes and decimals, which JSON cannot hold as they are."""
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


@server.tool(annotations=READ_ONLY)
def get_schema() -> dict:
    """Columns and types of the activities table, read from the database itself."""
    con = connect()
    try:
        columns = con.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position",
            [TABLE],
        ).fetchall()
        n_rows, first, last = con.execute(
            f"SELECT count(*), min(start_time_local), max(start_time_local) FROM {TABLE}"
        ).fetchone()
    finally:
        con.close()
    return {
        "table": TABLE,
        "columns": [{"name": name, "type": data_type} for name, data_type in columns],
        "rows": n_rows,
        "first_activity": to_json_value(first),
        "last_activity": to_json_value(last),
    }


@server.tool(annotations=READ_ONLY)
def get_notes() -> str:
    """Units, meaning of each column and tips for writing queries (garmin-llm's schema doc)."""
    return SCHEMA_DOC.read_text()


@server.tool(annotations=READ_ONLY)
def run_query(sql: str, limit: int = DEFAULT_LIMIT) -> dict:
    """Run one read-only DuckDB SQL query on the activities table.

    Returns the column names and up to `limit` rows (at most 2000).
    `truncated` is true when the result had more rows than that.
    Prefer aggregates (sum, avg, group by week) over fetching many rows.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    con = connect()
    # DuckDB has no query timeout setting, so a timer interrupts the query.
    timer = threading.Timer(TIMEOUT_S, con.interrupt)
    timer.start()
    try:
        cursor = con.execute(sql)
        columns = [column[0] for column in cursor.description]
        # One row more than the limit shows whether the result was cut off.
        rows = cursor.fetchmany(limit + 1)
    except duckdb.InterruptException:
        raise ToolError(f"query stopped after {TIMEOUT_S} s") from None
    except duckdb.Error as exc:
        # ToolError passes the message on to Claude, so it can fix the query.
        # Other exceptions reach the client only as a generic error.
        raise ToolError(str(exc)) from None
    finally:
        timer.cancel()
        con.close()
    return {
        "columns": columns,
        "rows": [[to_json_value(v) for v in row] for row in rows[:limit]],
        "truncated": len(rows) > limit,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.http:
        # Stateless with plain JSON replies: every tool call is one request and
        # one response, so the server keeps no sessions between calls.
        server.run("streamable-http", host="127.0.0.1", port=args.port, stateless_http=True, json_response=True)
    else:
        server.run()
