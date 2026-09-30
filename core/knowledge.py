"""
Local knowledge index — makes the assistant answerable from YOUR files.

The gap this fills: `file_processor` can only act on a file the user explicitly
names or drops on the interface. Nothing in the app could answer "what did I
write about the Sharma contract?" because no code ever looked at the Documents
folder. That is the whole of this module.

Design decisions worth stating, because each of them is a way this fails badly
if chosen differently:

  * Storage is SQLite, not a JSON blob. An embedding is 768 floats; ten thousand
    chunks is a ~30 MB file that has to be re-read and re-parsed on every query.
    SQLite keeps it in one file, updates a single file in place, and does not
    have to be loaded to answer a question.

  * Updates are incremental, keyed on (mtime, size). Re-indexing 10,000 files
    because one of them was edited is the difference between a feature and a
    thing nobody runs twice.

  * Embeddings come from Gemini, and the LEXICAL SCORER IS NOT A STRETCH GOAL —
    it is the fallback that runs whenever there is no key, no network, or an
    embedding call fails. A local index that stops working the moment the
    network hiccups is not something anyone can rely on, and the fallback costs
    no tokens because it is plain counting.

  * ON WHAT LEAVES THE MACHINE — this is the honest version. The index itself is
    a local file and the text is extracted locally, BUT building the vector part
    of the index means sending chunk text to Google's embedding endpoint, so
    indexing a folder does upload its contents. Search then also puts the
    retrieved chunks into a prompt, which is the model talking to Google, which
    it already does with everything else it hears. The user should be told this
    plainly before indexing a folder, because "my files never leave my machine"
    is a reasonable assumption to have and a false one to leave in place. If the
    key is absent or the network is down, the lexical scorer still answers from
    the local index with nothing sent anywhere at all.

    The escape hatch is indexing_guarded/reindex(embed=False): a purely local
    index that never contacts Google, with worse recall and no upload.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import struct
import sys
import threading
import time
from pathlib import Path

import json

# ── Tunables ──────────────────────────────────────────────────────────────────
# Chunk size is measured in characters, not tokens: it is the unit the extractor
# actually produces, and 1200 is roughly a paragraph-and-a-half, which is small
# enough that a retrieved chunk answers a question and large enough that it
# carries the context needed to answer it.
CHUNK_CHARS   = 1200
CHUNK_OVERLAP = 150
MAX_FILE_MB   = 4          # above this a "document" is a log dump or a dataset
MAX_FILES     = 20_000     # runaway guard; normal use is two orders below this

DB_PATH = Path(__file__).resolve().parent.parent / "memory" / "knowledge.db"

# Folders that are never what someone means by "my files". Skipping them is
# also what keeps the walk from spending minutes inside node_modules.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".next", ".nuxt", "target", ".gradle", ".idea", ".vscode",
    "AppData", "$RECYCLE.BIN", "System Volume Information", "Windows",
    "Program Files", "Program Files (x86)", "OneDrive", ".cache",
    "site-packages", ".npm", ".bun", "ms-playwright",
}

TEXT_EXT = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".jsonl", ".xml", ".yaml", ".yml", ".ini", ".cfg", ".toml", ".conf",
    ".html", ".htm", ".css", ".sql", ".srt", ".py", ".js", ".mjs", ".ts",
    ".tsx", ".jsx", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs",
    ".rb", ".php", ".swift", ".kt", ".sh", ".ps1", ".bat", ".lua", ".r",
}

# ── Storage ───────────────────────────────────────────────────────────────────

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")     # a re-index is a long write
    con.execute("""
        CREATE TABLE IF NOT EXISTS files (
            path       TEXT PRIMARY KEY,
            mtime      REAL,
            size       INTEGER,
            chunks     INTEGER,
            indexed_at REAL
        )""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            path  TEXT NOT NULL,
            ord   INTEGER NOT NULL,
            text  TEXT NOT NULL
        )""")
    con.execute("CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path)")
    # Vectors live apart from the text so a text-only lexical query never has to
    # read a single float. The key is (chunk_id, space) so a chunk can be
    # embedded by more than one backend at once.
    con.execute("""
        CREATE TABLE IF NOT EXISTS vectors (
            chunk_id INTEGER NOT NULL,
            dim      INTEGER NOT NULL,
            vec      BLOB NOT NULL,
            space    TEXT,
            PRIMARY KEY (chunk_id, space)
        )""")
    # Which model produced a vector, and therefore what its numbers mean.
    #
    # This column did not exist and its absence was a live hazard rather than a
    # tidiness complaint: search() scored every stored vector against the query
    # with a zip(), which stops at the shorter of the two. A 384-dim local vector
    # and a 768-dim Gemini vector therefore produced a confident-looking score
    # computed from the first 384 components of a different model's output. No
    # error, no warning, just wrong answers that look like working search.
    #
    # The row count backed that up: the old index held 832 vectors at 768 dims
    # alongside 60 at the full 3072, so that truncation was scoring 60 vectors
    # that had no business being compared at all.
    cols = {r[1] for r in con.execute("PRAGMA table_info(vectors)")}
    pk_cols = {r[1] for r in con.execute("PRAGMA table_info(vectors)") if r[5]}

    if "space" not in cols:
        con.execute("ALTER TABLE vectors ADD COLUMN space TEXT")
        # Backfill from the dimension, which is the only evidence the old rows
        # carry about their origin. 768 and 3072 are Gemini at two different
        # output sizes; anything else predates the local backend and is labelled
        # as unknown rather than guessed at.
        con.execute("UPDATE vectors SET space = "
                    "CASE WHEN dim IN (768, 3072) THEN 'gemini-' || dim "
                    "ELSE 'legacy-' || dim END WHERE space IS NULL")
        con.commit()

    # The space column alone was not enough. The original table was keyed on
    # chunk_id alone, so INSERT OR REPLACE deleted the Gemini row the moment a
    # local row for the same chunk was written — the two backends could never
    # coexist, and switching backend silently blanked the other index. Rebuild
    # once with the composite key.
    if pk_cols == {"chunk_id"}:
        con.execute("ALTER TABLE vectors RENAME TO vectors_oldkey")
        con.execute("""
            CREATE TABLE vectors (
                chunk_id INTEGER NOT NULL,
                dim      INTEGER NOT NULL,
                vec      BLOB NOT NULL,
                space    TEXT,
                PRIMARY KEY (chunk_id, space)
            )""")
        con.execute("""INSERT OR REPLACE INTO vectors (chunk_id, dim, vec, space)
                       SELECT chunk_id, dim, vec, space FROM vectors_oldkey""")
        con.execute("DROP TABLE vectors_oldkey")
        con.commit()
    con.execute("CREATE INDEX IF NOT EXISTS idx_vectors_space ON vectors(space)")
    return con


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


# ── Text extraction ───────────────────────────────────────────────────────────
# Every extractor returns text or raises. A file we cannot read is skipped and
# counted, never fatal — one encrypted PDF should not fail a 10,000-file walk.

def _read_plain(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def _read_pdf(path: Path) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(str(path)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages)
    except Exception:
        pass
    try:
        from PyPDF2 import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    except Exception:
        return ""


def _read_docx(path: Path) -> str:
    try:
        import docx
        return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    except Exception:
        return ""


def _read_pptx(path: Path) -> str:
    try:
        from pptx import Presentation
        out = []
        for slide in Presentation(str(path)).slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    out.append(shape.text_frame.text)
        return "\n".join(out)
    except Exception:
        return ""


def _read_xlsx(path: Path) -> str:
    try:
        import openpyxl
        wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            out.append(f"# sheet: {ws.title}")
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i > 2000:            # a spreadsheet is not a document
                    break
                out.append(" | ".join("" if c is None else str(c) for c in row))
        wb.close()
        return "\n".join(out)
    except Exception:
        return ""


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    try:
        if ext in TEXT_EXT:
            return _read_plain(path)
        if ext == ".pdf":
            return _read_pdf(path)
        if ext == ".docx":
            return _read_docx(path)
        if ext == ".pptx":
            return _read_pptx(path)
        if ext in (".xlsx", ".xlsm"):
            return _read_xlsx(path)
    except Exception:
        return ""
    return ""


# ── Chunking ──────────────────────────────────────────────────────────────────
# Split on paragraph boundaries where possible. A chunk that starts mid-sentence
# is a chunk the model quotes back awkwardly.

_PARAGRAPH = re.compile(r"\n\s*\n")


def chunk_text(text: str, size: int = CHUNK_CHARS,
              overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []

    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    pos = 0
    while pos < len(text):
        end = min(pos + size, len(text))
        if end < len(text):
            # Back up to the last blank line inside the window so the chunk ends
            # on a boundary; if there is none, cut on the last space.
            window = text[pos:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind(" "))
            if cut > size // 2:
                end = pos + cut + 1
        chunks.append(text[pos:end].strip())
        if end >= len(text):
            break
        pos = end - overlap
        if pos <= 0:
            pos = end
    return [c for c in chunks if len(c) > 40]


# ── Embeddings ────────────────────────────────────────────────────────────────
# Two model names because the newer one is not on every key yet; a free-tier key
# created a while ago 404s on it. Falling back costs one extra failed call at
# index time and nothing at query time.
_EMBED_MODELS = ("gemini-embedding-001", "text-embedding-004")
_model_cache: str | None = None
_dead_models: set = set()     # 404'd once, never tried again this process

# Measured against a free-tier key, not guessed:
#   batch of 16 x 300 words  → OK
#   batch of 32 x 300 words  → 429 RESOURCE_EXHAUSTED
# So the limit is tokens-per-minute, not items-per-request, and the fix is
# small batches plus a pause — not a bigger batch with a retry.
EMBED_DIMS      = 768     # Matryoshka truncation: 3072 → 768 is 4x less disk
                           # and loses nothing measurable for retrieval.
MAX_EMBED_CHARS = 1000    # the model truncates long inputs anyway; sending the
                           # full chunk just burns quota to produce a vector
                           # that ignores the tail.
EMBED_BATCH     = 12
EMBED_PAUSE     = 0.6     # seconds between batches, to stay under TPM
_BACKOFF        = (3, 8, 20)

# A free-tier key embeds at roughly 1.5 chunks/second once the rate limiter is
# respected, so a 20,000-chunk personal index is a multi-hour job. The pass is
# therefore BUDGETED and RESUMABLE rather than allowed to run to completion
# inside one tool call: it embeds until the budget runs out and stops, and the
# next call picks up exactly where it left off. Search is unaffected meanwhile —
# unembedded chunks are simply matched by keyword instead of by meaning.
EMBED_TIME_BUDGET  = 240.0     # seconds per index_my_files call
EMBED_MAX_STALLS   = 3         # consecutive dead batches before giving up


def _client():
    from google import genai
    from memory.config_manager import get_gemini_key
    key = get_gemini_key()
    if not key:
        return None
    return genai.Client(api_key=key)


def _embed_once(client, model: str, texts: list[str]) -> list[list[float]]:
    from google.genai import types
    res = client.models.embed_content(
        model=model,
        contents=[t[:MAX_EMBED_CHARS] for t in texts],
        config=types.EmbedContentConfig(output_dimensionality=EMBED_DIMS),
    )
    return [e.values for e in res.embeddings]


def _local_space() -> str:
    """The local model's space name, without importing torch to find out."""
    try:
        from core import embed_local
        return embed_local.SPACE
    except Exception:
        return "__local_unavailable__"


def _backend() -> str:
    """Which embedding backend to use: 'local', 'gemini' or 'auto'.

    Read fresh on every call rather than cached, so toggling it takes effect
    without a restart — which matters because the whole point of the switch is
    that the user finds out their quota is gone and needs an answer now.
    """
    try:
        from memory.config_manager import load_api_keys
        want = str(load_api_keys().get("embedding_backend", "auto") or "auto")
        return want if want in ("local", "gemini", "auto") else "auto"
    except Exception:
        return "auto"


def preferred_space() -> str:
    """The space new vectors should be written in, or None for no vectors.

    Also the space search() must filter on. Returning the same value from both is
    the point: a query vector has to be compared only against vectors built the
    same way, and the cheapest way to guarantee that is to have exactly one
    function deciding which space is in play.
    """
    mode = _backend()
    if mode in ("auto", "local"):
        from core import embed_local
        if mode == "local" or embed_local.ready():
            return embed_local.SPACE
    if mode in ("auto", "gemini"):
        return "gemini-768"
    return None


def embed(texts: list[str], log=print) -> list[list[float] | None]:
    """Embed a batch, aligned to `texts` — None wherever a chunk could not be
    embedded.

    Alignment matters: the caller stores vectors against chunk ids, so a short
    result would silently mis-associate every embedding after the failure.

    All-None means no backend was able to serve this batch (no key, offline,
    quota spent, local model still loading) and the caller should fall back to
    lexical scoring. A None in the middle means one chunk is the problem and the
    rest indexed fine, so the run continues.

    Every vector this returns belongs to preferred_space(), and nothing here
    mixes two spaces in one result — that is the whole reason that function
    exists.
    """
    global _model_cache
    if not texts:
        return []

    mode = _backend()
    space = preferred_space()
    if space is None:
        return [None] * len(texts)

    # ── Local ────────────────────────────────────────────────────────────────
    if space == _local_space():
        from core import embed_local
        got = embed_local.embed(texts, log=log)
        if got is not None and len(got) == len(texts):
            return got
        # The model is still loading or failed. Warming here is deliberate: a
        # long index run will get many chances, and the twenty seconds should be
        # spent in a background thread rather than discovered later mid-answer.
        if mode == "auto":
            embed_local.warm(log)
            return [None] * len(texts)
        if mode == "local":
            log(f"[Knowledge] Local backend unavailable "
                f"({embed_local.state()}: {embed_local.error()}) — keyword search "
                f"is unaffected.")
            return [None] * len(texts)

    # ── Gemini ───────────────────────────────────────────────────────────────
    if mode == "local":
        return [None] * len(texts)

    try:
        client = _client()
    except Exception:
        return [None] * len(texts)
    if client is None:
        return [None] * len(texts)

    out: list[list[float] | None] = [None] * len(texts)
    for model in (_model_cache, *_EMBED_MODELS):
        if not model or model in _dead_models:
            continue
        if all(v is not None for v in out):
            break
        pending = [i for i, v in enumerate(out) if v is None]
        rate_limited = False
        for start in range(0, len(pending), EMBED_BATCH):
            idxs = pending[start:start + EMBED_BATCH]
            batch = [texts[i] for i in idxs]
            for attempt, wait in enumerate((0,) + _BACKOFF):
                if wait:
                    time.sleep(wait)
                try:
                    vecs = _embed_once(client, model, batch)
                    for i, v in zip(idxs, vecs):
                        out[i] = v
                    _model_cache = model
                    time.sleep(EMBED_PAUSE)
                    break
                except Exception as e:
                    msg = str(e)
                    if "429" in msg or "RESOURCE_EXHAUSTED" in msg:
                        # A quota limit is not specific to this model, so trying
                        # the fallback would only spend another full backoff to
                        # learn the same thing. Stop and let the caller degrade.
                        rate_limited = True
                        break
                    if "404" in msg or "NOT_FOUND" in msg:
                        # This one really is unavailable for the key in hand.
                        _dead_models.add(model)
                        break
                    if attempt == len(_BACKOFF):
                        # One stubborn batch must not abandon the rest of the
                        # pass — it is left unembedded and picked up next time.
                        log(f"[Knowledge] batch skipped: "
                            f"{type(e).__name__} {msg[:100]}")
            time.sleep(EMBED_PAUSE)
            if rate_limited:
                log("[Knowledge] Embedding quota reached — pausing. "
                    "Keyword search is unaffected; re-run to finish.")
                return out
    return out




# ── Lexical fallback ──────────────────────────────────────────────────────────
# BM25 over the chunk table. Not a consolation prize: for a query containing a
# rare token — an error code, a person's surname, a file id — this frequently
# beats a generic embedding, because the embedding has no idea what that string
# means while the string itself is a perfect match.

_WORD = re.compile(r"[a-z0-9_]{2,}")
_STOP = {
    "the", "and", "for", "that", "this", "with", "you", "are", "was", "have",
    "from", "not", "but", "all", "can", "what", "when", "how", "why", "did",
    "does", "your", "our", "its", "his", "her", "they", "them", "been", "were",
    "there", "their", "would", "could", "should", "about", "which", "will",
}

_K1 = 1.5
_B = 0.75


def _tokenize(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def lexical_search(con: sqlite3.Connection, query: str, limit: int) -> list[tuple]:
    terms = _tokenize(query)
    if not terms:
        return []
    total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    if not total:
        return []

    scores: dict[int, float] = {}
    avg_len = con.execute("SELECT AVG(LENGTH(text)) FROM chunks").fetchone()[0] or 1.0

    for term in terms:
        rows = con.execute(
            "SELECT c.id, c.text FROM chunks c WHERE LOWER(c.text) LIKE ? LIMIT 4000",
            (f"%{term}%",),
        ).fetchall()
        if not rows:
            continue
        n_docs = len(rows)
        idf = math.log(1 + (total - n_docs + 0.5) / (n_docs + 0.5))
        for cid, text in rows:
            toks = _tokenize(text)
            if not toks:
                continue
            tf = toks.count(term)
            if not tf:
                continue
            norm = tf * (_K1 + 1) / (
                tf + _K1 * (1 - _B + _B * len(toks) / avg_len)
            )
            scores[cid] = scores.get(cid, 0.0) + idf * norm

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    if not ranked:
        return []
    ids = [cid for cid, _ in ranked]
    qmarks = ",".join("?" * len(ids))
    texts = dict(con.execute(
        f"SELECT id, text FROM chunks WHERE id IN ({qmarks})", ids).fetchall())
    paths = dict(con.execute(
        f"SELECT id, path FROM chunks WHERE id IN ({qmarks})", ids).fetchall())
    return [(paths[i], texts[i], s) for i, s in ranked if i in texts]


# ── Indexing ──────────────────────────────────────────────────────────────────

def default_folders() -> list[Path]:
    home = Path.home()
    names = ["Documents", "Desktop", "Downloads"]
    out = [home / n for n in names if (home / n).is_dir()]
    return out or [home]


def _walk(roots: list[Path], log=print) -> tuple[int, int, int]:
    """Return (files indexed, chunks written, files skipped)."""
    con = _connect()
    indexed = skipped = chunks_written = 0

    try:
        seen: set[str] = set()
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
                dirnames[:] = [d for d in dirnames
                               if d not in SKIP_DIRS and not d.startswith(".")
                               and d.lower() not in ("windows", "program files")]
                for fn in filenames:
                    if fn.startswith(".") or fn.startswith("~$"):
                        continue
                    p = Path(dirpath) / fn
                    if indexed + skipped >= MAX_FILES:
                        log(f"[Knowledge] File cap reached at {MAX_FILES}")
                        return indexed, chunks_written, skipped
                    try:
                        st = p.stat()
                    except OSError:
                        skipped += 1
                        continue
                    if st.st_size > MAX_FILE_MB * 1024 * 1024:
                        skipped += 1
                        continue
                    key = str(p).lower()
                    if key in seen:
                        continue
                    seen.add(key)

                    row = con.execute(
                        "SELECT mtime, size FROM files WHERE path = ?", (key,)
                    ).fetchone()
                    if row and abs(row[0] - st.st_mtime) < 1 and row[1] == st.st_size:
                        continue                      # unchanged, skip

                    text = extract_text(p)
                    if not text or len(text.strip()) < 40:
                        if row:
                            _drop_file(con, key)
                            con.execute(
                                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)",
                                (key, st.st_mtime, st.st_size, 0, time.time()))
                        skipped += 1
                        continue

                    pieces = chunk_text(text)
                    _drop_file(con, key)
                    for i, piece in enumerate(pieces):
                        con.execute(
                            "INSERT INTO chunks (path, ord, text) VALUES (?,?,?)",
                            (str(p), i, piece))
                        chunks_written += 1
                    con.execute(
                        "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)",
                        (key, st.st_mtime, st.st_size, len(pieces), time.time()))
                    indexed += 1
                    if indexed % 50 == 0:
                        con.commit()
                        log(f"[Knowledge] {indexed} files...")
    finally:
        con.commit()

    return indexed, chunks_written, skipped


def embed_and_store(log=print, max_seconds: float = EMBED_TIME_BUDGET) -> int:
    """Embed chunks that have no vector in the active space yet, in batches,
    inside a time budget.

    "In the active space" is the important qualifier. Resumability is scoped to
    one space, not to "has a vector": a chunk carrying a 768-dim Gemini vector
    still needs a 384-dim local one, and treating the first as satisfying the
    second would leave the local index permanently at 12% while reporting itself
    complete.

    Resumable by construction: it selects only chunks with no vector row in the
    active space, so being interrupted — by the budget, a rate limit, a crash, or
    Ctrl-C — costs at most the batch in flight, never the run.
    """
    space = preferred_space()
    if space is None:
        log("[Knowledge] No embedding backend available — keyword search only.")
        return 0

    # The local model takes ~20s to import. Start it now, on a background thread,
    # so the budget below is spent embedding rather than waiting for torch.
    if space == _local_space():
        from core import embed_local
        if not embed_local.ready():
            embed_local.warm(log)
            log("[Knowledge] Local model is loading in the background — "
                "re-run in a minute to embed. Keyword search is unaffected.")
            return 0

    deadline = time.monotonic() + max_seconds
    con = _connect()
    try:
        pending = con.execute("""
            SELECT c.id, c.text FROM chunks c
            LEFT JOIN vectors v ON v.chunk_id = c.id AND v.space = ?
            WHERE v.chunk_id IS NULL LIMIT 20000
        """, (space,)).fetchall()
        if not pending:
            return 0

        BATCH = EMBED_BATCH * 4 if space != _local_space() else 128
        written = failed = stalls = 0
        for i in range(0, len(pending), BATCH):
            if time.monotonic() >= deadline:
                log(f"[Knowledge] Time budget reached at "
                    f"{written}/{len(pending)} — re-run to continue.")
                break
            batch = pending[i:i + BATCH]
            vecs = embed([t for _, t in batch], log=log)
            got = 0
            for (cid, _), vec in zip(batch, vecs):
                if vec is None:
                    failed += 1
                    continue
                con.execute("INSERT OR REPLACE INTO vectors "
                            "(chunk_id, dim, vec, space) VALUES (?,?,?,?)",
                            (cid, len(vec), _pack(vec), space))
                written += 1
                got += 1
            con.commit()

            if got:
                stalls = 0
                log(f"[Knowledge] Embedded {written}/{len(pending)} chunks "
                    f"[{space}]")
            else:
                stalls += 1
                if stalls >= EMBED_MAX_STALLS:
                    log("[Knowledge] Embedding backend unavailable — stopping. "
                        "Keyword search still works; re-run later to finish.")
                    break
        return written
    finally:
        con.close()


def _drop_file(con: sqlite3.Connection, key: str) -> None:
    ids = [r[0] for r in con.execute(
        "SELECT id FROM chunks WHERE path = ?", (key,)).fetchall()]
    if ids:
        qmarks = ",".join("?" * len(ids))
        con.execute(f"DELETE FROM vectors WHERE chunk_id IN ({qmarks})", ids)
        con.execute(f"DELETE FROM chunks WHERE id IN ({qmarks})", ids)
    # chunks.path stores the display path, files.path the lowercased key, so the
    # delete below has to match on the lowercased form too.
    con.execute("DELETE FROM chunks WHERE LOWER(path) = ?", (key,))


_index_lock   = threading.Lock()
_index_running = False


def reindex_guarded(roots: list[Path] | None = None, log=print,
                    embed: bool = True) -> dict:
    """reindex(), but refuses to start a second pass while one is running.

    The index lives in one SQLite file and a concurrent walker would interleave
    deletes and inserts over the same rows, so a second caller is told to wait
    rather than being allowed to corrupt the first. The state lives here rather
    than in the action file because both actions reach it and neither should own
    a lock the other cannot see.
    """
    global _index_running
    with _index_lock:
        if _index_running:
            return {"error": "An index is already being built — give it a moment."}
        _index_running = True
    try:
        return reindex(roots, log=log, embed=embed)
    finally:
        with _index_lock:
            _index_running = False


def reindex(roots: list[Path] | None = None, log=print,
            embed: bool = True) -> dict:
    """Full pass: extract, store, then embed. Returns a stats dict.

    embed=False builds the index without ever contacting Google, leaving the
    lexical scorer as the only search path. Extraction is identical either way —
    only the remote vector step is skipped, which is what makes this a real
    privacy option rather than a degraded no-op.
    """
    global rows_for_vec
    rows_for_vec = []
    roots = roots or default_folders()
    roots = [Path(r) for r in roots if Path(r).is_dir()]
    if not roots:
        return {"error": "No indexable folder found."}

    t0 = time.time()
    indexed, chunks_written, skipped = _walk(roots, log=log)
    embedded = embed_and_store(log=log) if embed else 0

    con = _connect()
    total_files = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    total_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    have_vecs = con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
    con.close()

    return {
        "roots": [str(r) for r in roots],
        "files_indexed": indexed,
        "files_skipped": skipped,
        "chunks_written": chunks_written,
        "chunks_embedded": embedded,
        "total_files": total_files,
        "total_chunks": total_chunks,
        "vectors": have_vecs,
        "seconds": round(time.time() - t0, 1),
    }


# ── Query ─────────────────────────────────────────────────────────────────────

def search(query: str, limit: int = 6, log=print) -> list[dict]:
    """Return the best-matching chunks. Semantic when embeddings exist, lexical
    otherwise, and the union of both when they disagree — they fail in different
    directions, so a chunk either one ranks highly is usually worth reading."""
    query = (query or "").strip()
    if not query:
        return []

    con = _connect()
    try:
        results: dict[str, dict] = {}

        space = preferred_space()
        vecs = embed([query], log=log) if space else [None]
        qvec = vecs[0] if vecs else None
        if qvec:
            # Scoped to one space, always. See the note on the vectors.space
            # column: comparing across spaces yields a plausible number and a
            # wrong answer.
            rows = con.execute("""
                SELECT c.id, c.path, c.text, v.vec
                FROM chunks c JOIN vectors v ON v.chunk_id = c.id
                WHERE v.space = ? LIMIT 20000""", (space,)).fetchall()
            for rank, (s, cid, path, text) in enumerate(
                    _rank_semantic(rows, qvec, limit)):
                results[f"{path}#{cid}"] = {
                    "path": path, "text": text,
                    "score": round(s, 4), "rank": rank, "via": "semantic",
                }

        for path, text, s in lexical_search(con, query, limit):
            k = path + "#lex"
            if k in results:
                continue
            results[k] = {"path": path, "text": text,
                          "score": round(s, 3), "rank": len(results), "via": "keyword"}

        out = sorted(results.values(), key=lambda r: r["rank"])[:limit]

        # A file with no vector and no keyword hit should not be able to answer,
        # so an empty index says so plainly rather than returning nothing.
        if not out:
            total = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            if not total:
                return [{"path": "", "text": (
                    "The local knowledge index is empty. Build it once with the "
                    "index_my_files action, then ask again."), "score": 0.0,
                    "rank": 0, "via": "empty"}]
        return out
    finally:
        con.close()


def _rank_semantic(rows: list, qvec: list[float], limit: int) -> list[tuple]:
    """Score stored vectors against a query vector, best first.

    Returns [(score, chunk_id, path, text), ...], at most `limit` of them.

    Vectorised with numpy because the previous version was a Python loop doing
    len(rows) x dim multiply-adds per query — 5.7 million operations for this
    index, on the same thread that has to keep audio flowing. numpy is already a
    hard dependency of the avatar, so this costs nothing to import.

    The numpy path is a genuine fallback, not a nicety: a build without numpy
    still searches, just more slowly, and quietly rather than not at all.
    """
    if not rows or not qvec:
        return []
    try:
        import numpy as np
    except Exception:
        return _rank_semantic_python(rows, qvec, limit)

    dim = len(qvec)
    usable = [(cid, path, text, blob) for cid, path, text, blob in rows
              if blob is not None and len(blob) // 4 == dim]
    if not usable:
        # Everything stored is a different dimensionality. Say so by returning
        # nothing rather than by comparing what we can — a partial-length dot
        # product is the exact failure the space column exists to prevent.
        return []

    mat = np.frombuffer(b"".join(b for _, _, _, b in usable),
                        dtype="<f4").reshape(len(usable), dim)
    q = np.asarray(qvec, dtype=np.float32)
    n = float(np.linalg.norm(q)) or 1.0
    scores = (mat @ (q / n)) / np.maximum(
        np.linalg.norm(mat, axis=1), 1e-9)

    k = min(limit, len(usable))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(float(scores[i]), usable[i][0], usable[i][1], usable[i][2])
            for i in top]


def _rank_semantic_python(rows: list, qvec: list[float], limit: int) -> list[tuple]:
    """No-numpy fallback. Same contract, same dimensionality guard."""
    q = _unit(qvec)
    dim = len(q)
    scored = []
    for cid, path, text, blob in rows:
        if blob is None or len(blob) // 4 != dim:
            continue
        scored.append((_dot(_unpack(blob), q), cid, path, text))
    scored.sort(key=lambda r: r[0], reverse=True)
    return scored[:limit]


def _unit(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def stats() -> dict:
    con = _connect()
    try:
        spaces = {s: {"dim": d, "count": n} for s, d, n in con.execute(
            "SELECT space, dim, COUNT(*) FROM vectors GROUP BY space, dim")}
        return {
            "total_files":   con.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "total_chunks":  con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "vectors":       con.execute("SELECT COUNT(*) FROM vectors").fetchone()[0],
            # Per-space coverage, because a single vector total cannot tell you
            # whether the index you are about to search is the one you built.
            # 7,453 vectors is 100% in one space and 0% in the other.
            "spaces":        spaces,
            "active_space":  preferred_space(),
            "db_path":       str(DB_PATH),
        }
    finally:
        con.close()


if __name__ == "__main__":
    # python core/knowledge.py            → build/refresh the index
    # python core/knowledge.py "my query" → search it
    if len(sys.argv) > 1:
        for hit in search(sys.argv[1]):
            print(f"\n--- {hit['path']}  ({hit['via']} {hit['score']})")
            print(hit["text"][:400])
    else:
        print(json.dumps(reindex(), indent=2))
