"""
Database queries — ask questions of the user's own data in SQL.

The one tool the model reaches for here. Everything about running arbitrary SQL
against a live database is in core/databases.py: row capping, timeouts, the
read-only open for SQLite, and the write gate. This file is only the tool
declaration and the human-readable formatting.
"""
from core import databases


def query_action(parameters: dict, player=None, session_memory=None) -> str:
    sql = (parameters.get("sql") or "").strip()
    if not sql:
        return ("What should I run? Give me a SQL statement, e.g. "
                "SELECT status, COUNT(*) FROM etl_jobs GROUP BY status;")

    name = (parameters.get("connection") or "").strip() or None
    try:
        conn = databases.get(name)
    except KeyError as e:
        return (f"{e}\n\nSaved connections: "
                f"{[c.get('name') for c in databases.connections()] or 'none'}. "
                f"Ask the user to add one, or run list_databases to see what "
                f"can be connected to.")

    try:
        max_rows = int(parameters.get("max_rows") or databases.MAX_ROWS_DEFAULT)
    except (TypeError, ValueError):
        max_rows = databases.MAX_ROWS_DEFAULT
    allow_write = bool(parameters.get("allow_write"))

    print(f"[Database] {conn.get('name')}: {sql[:200]}")
    try:
        result = databases.run(conn, sql, max_rows=max_rows,
                               allow_write=allow_write)
    except databases.QueryError as e:
        return f"That query didn't run: {e}"
    except Exception as e:
        return f"Database error ({type(e).__name__}): {e}"

    return databases.format_table(result, conn.get("name", "db"))


TOOL = {
    "name": "query_database",
    "description": (
        "Run a SQL query against one of the user's own databases — SQLite files "
        "or PostgreSQL servers they have connected. Use this whenever they ask a "
        "question whose answer lives in data rather than on the web: how many "
        "rows, what failed, totals and sums, the latest entries, anything by "
        "status/date/owner. Read-only by default: a query that INSERTs, UPDATEs, "
        "DELETEs, DROPs or ALTERs is refused, so if the user has asked for a "
        "change, check with them first and then pass allow_write=true. One "
        "statement per call. Before writing SQL, call list_databases if you do "
        "not know the connection names or the table names."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "sql": {
                "type": "STRING",
                "description": ("A single SQL statement. Standard SQL; for "
                                "PostgreSQL also accept its dialect."),
            },
            "connection": {
                "type": "STRING",
                "description": ("Which saved connection to use. Leave empty for "
                                "the default. Get the names from "
                                "list_databases."),
            },
            "max_rows": {
                "type": "INTEGER",
                "description": ("How many rows to return (default 100, max "
                                "1000). Use a small number with a question "
                                "that needs a total — the SQL can aggregate."),
            },
            "allow_write": {
                "type": "BOOLEAN",
                "description": ("Set true ONLY after the user has confirmed a "
                                "change they asked for. Leave false otherwise."),
            },
        },
        "required": ["sql"],
    },
    "handler": query_action,
}
