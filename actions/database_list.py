"""
Database connections — see what can be queried.

Split from actions/database_query.py because core/action_loader.py takes one
TOOL dict per file. This is the one the model calls first: guessing a table name
it has never seen is the most common way a text-to-SQL assistant produces a
confident answer to the wrong question.
"""
from core import databases


def list_action(parameters: dict, player=None, session_memory=None) -> str:
    lines = []
    saved = databases.connections()
    default = databases.default_name()

    if saved:
        lines.append("Saved connections:")
        for c in saved:
            kind = (c.get("type") or "sqlite").lower()
            where = (c.get("path") if kind == "sqlite"
                     else f"{c.get('host')}:{c.get('port') or 5432}/"
                          f"{c.get('database')}")
            mark = " (default)" if c.get("name") == default else ""
            lines.append(f"  - {c.get('name')}{mark}  [{kind}]  {where}")
    else:
        lines.append("No connections saved yet.")

    # SQLite files need no credentials at all, so they are always worth
    # surfacing — even when other connections exist, because the data the user
    # means is often in a project folder nobody connected on purpose.
    try:
        found = databases.discover_sqlite()
    except Exception:
        found = []
    if found:
        head = ("\nSQLite files on this machine — any of these can be connected "
                "to with no password:"
                if not saved else
                "\nOther SQLite files found (not yet connected):")
        lines.append(head)
        for f in found[:15]:
            lines.append(f"  - {f['path']}  ({f['kb']} KB)")
        if len(found) > 15:
            lines.append(f"  … and {len(found) - 15} more")
        if not saved:
            lines.append("\nCall connect_database with one of those paths to "
                         "start querying it.")

    if saved:
        lines.append("\nCall query_database with one of these names. If you "
                     "need the table names, run a query like SELECT name FROM "
                     "sqlite_master WHERE type='table' (SQLite) or SELECT "
                     "table_name FROM information_schema.tables WHERE "
                     "table_schema='public' (Postgres).")

    return "\n".join(lines)


TOOL = {
    "name": "list_databases",
    "description": (
        "List the database connections the user has, where they point, and any "
        "SQLite files found on this machine that are not connected yet. Call "
        "this BEFORE writing any SQL when you do not already know the "
        "connection name and the table names — it is how you find out that the "
        "data lives in 'etl' rather than in whatever name you would have "
        "guessed."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": [],
    },
    "handler": list_action,
}
