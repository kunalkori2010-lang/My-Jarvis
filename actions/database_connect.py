"""
Database connections — adding a new one.

Split from actions/database_list.py because core/action_loader.py takes one TOOL
dict per file. The connection is tested before it is saved, so a wrong password
is reported now instead of turning every later query into a mystery.
"""
from pathlib import Path

from core import databases


def connect_action(parameters: dict, player=None, session_memory=None) -> str:
    kind = (parameters.get("type") or "sqlite").lower()

    if kind == "sqlite":
        path = (parameters.get("path") or "").strip().strip('"')
        if not path:
            return "Give me the full path to the .db file."
        p = Path(path).expanduser()
        if not p.exists():
            return f"There's no file at {p}."
        name = (parameters.get("name") or p.stem).strip()
        conn = {"name": name, "type": "sqlite", "path": str(p)}
    else:
        name = (parameters.get("name") or "").strip()
        if not name:
            return "Give the connection a name, e.g. 'appdb'."
        try:
            port = int(parameters.get("port") or 5432)
        except (TypeError, ValueError):
            port = 5432
        conn = {
            "name": name,
            "type": "postgres",
            "host": (parameters.get("host") or "localhost").strip(),
            "port": port,
            "database": (parameters.get("database") or name).strip(),
            "user": (parameters.get("user") or "postgres").strip(),
            "password": parameters.get("password") or "",
        }

    try:
        probe = databases.run(conn, "SELECT 1", max_rows=1)
    except Exception as e:
        return (f"I couldn't connect: {e}\nNothing was saved. Check the "
                f"details — for PostgreSQL the password has to come from the "
                f"user, not from a guess.")

    databases.save_connection(conn)
    msg = (f"Connected to '{conn['name']}' ({probe['ms']} ms). "
           f"You can query it now.")
    print(f"[Database] {msg}")
    if player:
        try:
            player.write_log(f"JARVIS: {msg}")
        except Exception:
            pass
    return msg


TOOL = {
    "name": "connect_database",
    "description": (
        "Save a database connection so queries can be run against it. A SQLite "
        "file needs only its path. PostgreSQL needs host, port, database, user "
        "and password — ask the user for the password rather than guessing, and "
        "tell them it will be stored in plaintext in config/api_keys.json "
        "alongside their other credentials. The connection is tested before "
        "being saved."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "A short name for this connection, e.g. 'etl'.",
            },
            "type": {
                "type": "STRING",
                "description": "'sqlite' or 'postgres'.",
            },
            "path": {
                "type": "STRING",
                "description": "For SQLite: full path to the .db file.",
            },
            "host": {"type": "STRING", "description": "Postgres host."},
            "port": {"type": "INTEGER", "description": "Postgres port (5432)."},
            "database": {"type": "STRING",
                         "description": "Postgres database name."},
            "user": {"type": "STRING", "description": "Postgres user."},
            "password": {"type": "STRING",
                         "description": ("Postgres password. Ask the user for "
                                         "it; never invent one.")},
        },
        "required": ["name", "type"],
    },
    "handler": connect_action,
}
