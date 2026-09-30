"""
Database access — let the assistant answer questions about the user's own data.

Why this is a core module and not three plugins: SQLite needs no credentials and
Postgres needs them, the two have genuinely different safety models, and both
need the same row capping and timeout. Splitting that across plugin files is how
you end up with a tool that can print 400,000 rows into a voice conversation.

Supported: SQLite (stdlib, zero setup) and PostgreSQL (psycopg2). Connections
live in config/api_keys.json under "databases", which is already git-ignored.

Writes are permitted — this is the user's machine and their data — but they are
gated on an explicit `allow_write`, because the caller is a language model
listening to a microphone. The gate is not a security boundary: it exists so the
model has to ask before it drops a table, exactly as it would before shutting
the computer down. Once it has asked, nothing stops it.
"""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from memory.config_manager import _patch_config, load_api_keys

MAX_ROWS_DEFAULT = 100
MAX_ROWS_CEILING = 1000
MAX_CELL_CHARS   = 60          # a 4 KB cell in a voice answer is unreadable
QUERY_TIMEOUT_S  = 20.0

# Statements that change data or schema. Matched on the first keyword after
# stripping comments and whitespace, so a leading "-- note" or "/* x */" does
# not disguise a DELETE.
_WRITE_RE = re.compile(
    r"^\s*(?:--[^\n]*\n|/\*.*?\*/\s*)*"
    r"(insert|update|delete|drop|truncate|alter|create|grant|revoke|"
    r"replace|merge|copy|vacuum|attach|detach)\b",
    re.IGNORECASE | re.DOTALL,
)

_COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_FIRST_WORD_RE = re.compile(r"^\s*([a-z]+)", re.IGNORECASE)


def strip_comments(sql: str) -> str:
    """Remove SQL comments so the verb of a statement is what it appears to be.

    Without this, "-- tidy up\nDELETE FROM x" reports an empty verb, and a
    message naming '' is both useless to the model and a hint that the
    detection itself is shaky.
    """
    return _COMMENT_RE.sub(" ", sql or "")


def first_word(sql: str) -> str:
    m = _FIRST_WORD_RE.match(strip_comments(sql))
    return m.group(1).lower() if m else ""


def is_write(sql: str) -> bool:
    return bool(_WRITE_RE.match(sql or ""))


# ── registry ──────────────────────────────────────────────────────────────────

def connections() -> list[dict]:
    cfg = load_api_keys().get("databases")
    conns = cfg.get("connections") if isinstance(cfg, dict) else None
    return [c for c in (conns or []) if isinstance(c, dict) and c.get("name")]


def default_name() -> str:
    cfg = load_api_keys().get("databases")
    if isinstance(cfg, dict):
        return cfg.get("default") or ""
    return ""


def get(name: str | None = None) -> dict:
    conns = connections()
    if name:
        for c in conns:
            if c.get("name") == name:
                return c
        raise KeyError(f"No connection named '{name}'. Known: "
                       f"{[c.get('name') for c in conns] or 'none saved'}")
    if not conns:
        raise KeyError("No database connections are configured yet.")
    d = default_name()
    for c in conns:
        if c.get("name") == d:
            return c
    return conns[0]


def save_connection(conn: dict) -> None:
    conns = [c for c in connections() if c.get("name") != conn.get("name")]
    conns.append(conn)
    _patch_config(databases={"connections": conns,
                             "default": default_name() or conn.get("name")})


def remove_connection(name: str) -> bool:
    conns = connections()
    kept = [c for c in conns if c.get("name") != name]
    if len(kept) == len(conns):
        return False
    _patch_config(databases={"connections": kept,
                             "default": default_name()})
    return True


# ── discovery ─────────────────────────────────────────────────────────────────
# Finding the SQLite files the user already has is worth doing automatically:
# a connection they never have to configure is a connection they will actually
# use, and these files are scattered across project folders.

def _walk_bounded(root: Path, max_depth: int, skip: set):
    """os.walk with a hard depth limit.

    Depth is counted from `root`, not from the drive letter, so a folder passed
    in as C:/Users/x/Desktop does not silently become a whole-profile scan
    because the path happens to be deep.
    """
    import os
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        depth = len(Path(dirpath).parts) - base
        dirnames[:] = [d for d in dirnames
                       if d not in skip and not d.startswith(".")
                       and d.lower() not in ("windows", "program files")
                       and depth < max_depth]
        yield dirpath, filenames


def discover_sqlite(roots: list[Path] | None = None,
                    max_depth: int = 4, limit: int = 40) -> list[dict]:
    """Find .db / .sqlite files worth offering, skipping the noisy ones."""
    if roots is None:
        home = Path.home()
        roots = [home / n for n in ("Desktop", "Documents", "Downloads",
                                     "projects", "code")
                 if (home / n).is_dir()] or [home]
    skip = {".git", "node_modules", "__pycache__", "AppData", ".venv",
            "venv", "site-packages", ".cache", "ms-playwright", ".npm"}
    found: list[dict] = []
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for dirpath, filenames in _walk_bounded(root, max_depth, skip):
            for fn in filenames:
                low = fn.lower()
                if not (low.endswith((".db", ".sqlite", ".sqlite3"))):
                    continue
                p = Path(dirpath) / fn
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                # Skip the WAL sidecars and the multi-GB tool databases; neither
                # is something anyone means by "my data".
                if size > 512 * 1024 * 1024:
                    continue
                found.append({"path": str(p), "kb": size // 1024})
    found.sort(key=lambda d: d["path"])
    return found[:limit]


# ── execution ─────────────────────────────────────────────────────────────────

class QueryError(Exception):
    pass


def _connect_sqlite(path: str, write: bool):
    p = Path(path).expanduser()
    if not p.exists():
        raise QueryError(f"No such database file: {p}")
    if write:
        con = sqlite3.connect(str(p), timeout=5)
    else:
        # mode=ro is enforced by SQLite itself, not by this code deciding to be
        # careful — so a bug in the write-detection cannot drop a table.
        con = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _connect_postgres(c: dict):
    try:
        import psycopg2
    except ImportError:
        raise QueryError("psycopg2 is not installed. "
                         "Run: pip install psycopg2-binary")
    try:
        return psycopg2.connect(
            host=c.get("host", "localhost"),
            port=int(c.get("port") or 5432),
            dbname=c.get("database") or c.get("name"),
            user=c.get("user") or "postgres",
            password=c.get("password") or None,
            connect_timeout=8,
        )
    except Exception as e:
        raise QueryError(f"Could not connect: {e}")


def _cap_sqlite(sql: str, limit: int) -> str:
    """Append LIMIT to a bare SELECT so one question cannot pull a whole table.

    Only applied when the statement is a single SELECT with no LIMIT of its own
    and no CTE — rewriting a query the user actually wrote is how you return
    subtly wrong results, so anything ambiguous is left alone and the row cap in
    the formatter does the containing instead.
    """
    s = sql.strip().rstrip(";").strip()
    if not re.match(r"^select\b", s, re.IGNORECASE):
        return sql
    if re.search(r"\blimit\b", s, re.IGNORECASE):
        return sql
    if re.search(r"\bwith\b", s, re.IGNORECASE) or ";" in s:
        return sql
    return f"{s} LIMIT {limit}"


def _make_deadline(budget: float = QUERY_TIMEOUT_S):
    """Progress handler that aborts once the deadline passes.

    sqlite3 aborts only when the handler returns a non-zero value; returning
    None means "carry on" and leaves the query running forever. Raising from
    inside the handler is also wrong — sqlite3 flattens it into a bare
    OperationalError("interrupted"), which tells the user nothing.
    """
    deadline = time.monotonic() + budget
    return lambda: 1 if time.monotonic() > deadline else 0


def _is_interrupt(exc: BaseException) -> bool:
    return "interrupt" in str(exc).lower()


def _timeout_msg() -> str:
    return (f"That query ran longer than {QUERY_TIMEOUT_S:.0f}s and was "
            f"cancelled, so nothing was changed. Try a more selective "
            f"statement — add a WHERE clause, a LIMIT, or aggregate instead of "
            f"returning every row.")


def table_names(conn: dict) -> list[str]:
    """Table names for a connection. Best effort — never raises."""
    kind = (conn.get("type") or "sqlite").lower()
    try:
        if kind == "sqlite":
            con = _connect_sqlite(conn.get("path", ""), write=False)
            try:
                return [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type IN "
                    "('table','view') AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY 1")]
            finally:
                con.close()
        if kind in ("postgres", "postgresql"):
            con = _connect_postgres(conn)
            try:
                with con.cursor() as cur:
                    cur.execute("SELECT table_name FROM information_schema."
                                "tables WHERE table_schema='public' "
                                "ORDER BY 1")
                    return [r[0] for r in cur.fetchall()]
            finally:
                con.close()
    except Exception:
        return []
    return []


def column_names(conn: dict, table: str) -> list[str]:
    """Column names for one table. Best effort — never raises.

    The companion to table_names(). Exists because "the table is there but the
    column is not" is the single most common shape of a query that silently
    returns nothing, and a caller cannot tell that apart from a real result of
    zero rows unless it can ask.
    """
    kind = (conn.get("type") or "sqlite").lower()
    # PRAGMA takes no bind parameter, so the identifier is quoted by hand.
    # Double quotes are the standard form and work in SQLite and Postgres; the
    # inner doubling is how both escape a quote inside an identifier.
    ident = '"' + str(table).replace('"', '""') + '"'
    try:
        if kind == "sqlite":
            con = _connect_sqlite(conn.get("path", ""), write=False)
            try:
                return [r[1] for r in con.execute(
                    f"PRAGMA table_info({ident})")]
            finally:
                con.close()
        if kind in ("postgres", "postgresql"):
            con = _connect_postgres(conn)
            try:
                with con.cursor() as cur:
                    cur.execute("SELECT column_name FROM "
                                "information_schema.columns WHERE "
                                "table_schema='public' AND table_name=%s "
                                "ORDER BY ordinal_position", (table,))
                    return [r[0] for r in cur.fetchall()]
            finally:
                con.close()
    except Exception:
        return []
    return []


def _explain_sqlite_error(e: Exception, conn: dict) -> str:
    """Turn 'no such table: x' into something the model can act on.

    A bare "no such table" is the single most common failure here and it is
    almost always a wrong *connection*, not a wrong table name — the data
    exists, just in the other database. Naming the other connection is the
    difference between one more tool call and a wrong answer to the user.
    """
    msg = str(e)
    m = re.search(r"no such table:\s*(\S+)", msg, re.IGNORECASE)
    if not m:
        return f"{type(e).__name__}: {msg}"
    table = m.group(1).strip("\"'")
    out = [f"There is no table called '{table}' in connection "
           f"'{conn.get('name')}'."]
    for other in connections():
        if other.get("name") == conn.get("name"):
            continue
        if table.lower() in [t.lower() for t in table_names(other)]:
            out.append(f"'{table}' does exist in connection "
                       f"'{other.get('name')}' — use that instead.")
            return " ".join(out)
    names = table_names(conn)
    if names:
        out.append(f"Tables available in '{conn.get('name')}': "
                   f"{', '.join(names[:25])}.")
    return " ".join(out)


def run(conn: dict, sql: str, max_rows: int = MAX_ROWS_DEFAULT,
        allow_write: bool = False) -> dict:
    """Execute one statement. Returns {columns, rows, rowcount, truncated, ms}."""
    sql = (sql or "").strip()
    if not sql:
        raise QueryError("No SQL was given.")

    first = first_word(sql)
    is_multi = ";" in sql.strip().rstrip(";")

    if is_write(sql) and not allow_write:
        raise QueryError(
            f"'{first.upper()}' changes data, so it was not run. Ask the user "
            f"to confirm first, then call this again with allow_write=true. "
            f"Nothing has been changed.")
    if is_multi:
        raise QueryError("Run one statement at a time — a multi-statement "
                         "string is refused rather than half-executed.")

    max_rows = max(1, min(int(max_rows or MAX_ROWS_DEFAULT), MAX_ROWS_CEILING))
    kind = (conn.get("type") or "sqlite").lower()
    t0 = time.monotonic()

    if kind == "sqlite":
        con = _connect_sqlite(conn.get("path", ""), write=allow_write)
        con.set_progress_handler(_make_deadline(), 2000)
        try:
            cur = con.execute(_cap_sqlite(sql, max_rows))
            if cur.description is None:
                con.commit()
                return {"columns": [], "rows": [],
                        "rowcount": cur.rowcount if cur.rowcount >= 0 else 0,
                        "truncated": False,
                        "ms": round((time.monotonic() - t0) * 1000)}
            cols = [d[0] for d in cur.description]
            fetched = cur.fetchmany(max_rows + 1)
            truncated = len(fetched) > max_rows
            rows = [list(r) for r in fetched[:max_rows]]
            return {"columns": cols, "rows": rows, "rowcount": len(rows),
                    "truncated": truncated,
                    "ms": round((time.monotonic() - t0) * 1000)}
        except sqlite3.OperationalError as e:
            # OperationalError subclasses DatabaseError, so this must come first
            # and must not re-raise the "no such table" case — that one is worth
            # explaining rather than passing through.
            if _is_interrupt(e):
                raise QueryError(_timeout_msg())
            raise QueryError(_explain_sqlite_error(e, conn))
        except sqlite3.DatabaseError as e:
            raise QueryError(f"{type(e).__name__}: {e}")
        finally:
            con.set_progress_handler(None, 0)
            con.close()

    if kind in ("postgres", "postgresql"):
        con = _connect_postgres(conn)
        try:
            with con.cursor() as cur:
                # Provider-side, so a runaway query is killed by the server even
                # if this process dies while waiting.
                cur.execute("SET statement_timeout = %s",
                            (int(QUERY_TIMEOUT_S * 1000),))
                cur.execute(sql)
                if cur.description is None:
                    con.commit()
                    return {"columns": [], "rows": [],
                            "rowcount": cur.rowcount,
                            "truncated": False,
                            "ms": round((time.monotonic() - t0) * 1000)}
                cols = [d[0] for d in cur.description]
                fetched = cur.fetchmany(max_rows + 1)
                truncated = len(fetched) > max_rows
                rows = [list(r) for r in fetched[:max_rows]]
            if not allow_write:
                con.rollback()
            else:
                con.commit()
            return {"columns": cols, "rows": rows, "rowcount": len(rows),
                    "truncated": truncated,
                    "ms": round((time.monotonic() - t0) * 1000)}
        except Exception as e:
            if _is_interrupt(e) or "timeout" in str(e).lower():
                raise QueryError(_timeout_msg())
            raise
        finally:
            con.close()

    raise QueryError(f"Unsupported connection type '{kind}'.")


# ── formatting ────────────────────────────────────────────────────────────────

def _cell(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    s = str(v).replace("\n", " ").replace("\r", " ")
    if len(s) > MAX_CELL_CHARS:
        s = s[:MAX_CELL_CHARS - 1] + "…"
    return s


def format_table(result: dict, conn_name: str) -> str:
    cols, rows = result.get("columns") or [], result.get("rows") or []
    if not cols:
        n = result.get("rowcount", 0)
        return f"{conn_name}: statement OK, {n} row(s) affected ({result['ms']} ms)."

    widths = [min(MAX_CELL_CHARS, max(len(c), 3)) for c in cols]
    body = []
    for r in rows:
        cells = [_cell(v) for v in r]
        for i, w in enumerate(widths[:len(cells)]):
            widths[i] = min(MAX_CELL_CHARS, max(w, len(cells[i])))
        body.append(cells)

    def line(cells):
        return " | ".join(c.ljust(widths[i])[:widths[i]]
                          for i, c in enumerate(cells[:len(widths)]))

    out = [line(cols), "-+-".join("-" * w for w in widths)]
    out += [line(c) for c in body]
    note = f"\n({len(rows)} row{'s' if len(rows) != 1 else ''}"
    note += ", more available — raise max_rows" if result.get("truncated") else ""
    note += f", {result['ms']} ms)"
    return f"{conn_name}:\n" + "\n".join(out) + note
