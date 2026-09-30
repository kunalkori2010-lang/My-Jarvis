"""
Local embeddings — the reason search still works when Google's quota is spent.

The problem this solves is narrow and real. The knowledge index is 7,453 chunks
across 837 files, and embedding all of them through Gemini costs quota that a
free-tier key simply does not have. When it runs out, 88% of the index has no
vector and the assistant quietly stops understanding what you asked, falling back
to keyword matching. That is the failure this removes: a ~90 MB model, run on the
CPU, with no key, no quota and no network after the first fetch.

Three things this module is careful about, because each one was a way to break a
running assistant:

  NOTHING BLOCKS. Importing torch costs about twenty seconds on this machine.
  Doing that inside a search would freeze the assistant mid-answer, so the import
  happens on a daemon thread started by warm(), and every entry point returns
  "not ready" until it finishes. The index keeps working on keywords throughout.

  IT IS OPTIONAL. No torch, no transformers, or no model on disk means ready() is
  False forever and every caller falls through to exactly the behaviour it had
  before this file existed. Nothing here is load-bearing for the app; it only
  makes the existing search better.

  ONE SPACE AT A TIME. A MiniLM vector and a Gemini vector are not comparable —
  different dimensions, different training, and the dot product between them is
  arithmetic, not meaning. Callers get a space name and must filter on it. The
  previous search did not filter on anything, which is why this module is only
  reachable behind an explicit space check in knowledge.search().

Model: all-MiniLM-L6-v2 — 384 dimensions, ~90 MB, CPU-viable, and the standard
choice for exactly this size of corpus. It is downloaded once into the normal
Hugging Face cache and works offline from then on.
"""
from __future__ import annotations

import threading

# ── Configuration ─────────────────────────────────────────────────────────────

MODEL_ID   = "sentence-transformers/all-MiniLM-L6-v2"
SPACE      = "minilm-l6-v2-384"
DIMS       = 384
BATCH      = 32
MAX_TOKENS = 256          # chunks are ~1000 chars; 256 tokens covers them and
                          # keeps a batch fast on 6 CPU threads

# Consecutive batch failures tolerated before the backend declares itself
# unavailable. A single bad batch is usually transient — an allocation spike, a
# chunk that trips a tokenizer edge case — and giving up on it would take search
# down for the rest of the session. Several in a row is a real fault, and staying
# 'ready' while failing every query would be a lie worth correcting.
MAX_FAILURES = 3

# ── State ─────────────────────────────────────────────────────────────────────
# "idle" -> "loading" -> "ready" | "failed" | "absent"
_state  = "idle"
_error  = ""
_lock   = threading.Lock()
_model  = None
_tok    = None
_fails  = 0


def _missing_deps() -> str:
    """Why this backend cannot run, or '' if it can. Never raises."""
    import importlib.util as u
    for mod in ("torch", "transformers"):
        if u.find_spec(mod) is None:
            return mod
    return ""


def state() -> str:
    return _state


def error() -> str:
    return _error


def ready() -> bool:
    """True only when a call to embed() will actually produce vectors."""
    return _state == "ready" and _model is not None and _tok is not None


def status() -> dict:
    return {"state": _state, "space": SPACE, "dims": DIMS,
            "model": MODEL_ID, "error": _error}


def warm(log=print) -> None:
    """Start loading the model on a background thread. Safe to call repeatedly.

    Called opportunistically — at startup and before an index run — so that by the
    time somebody asks a question the twenty seconds have already passed somewhere
    it does not matter.
    """
    global _state, _error
    with _lock:
        if _state in ("loading", "ready"):
            return
        _state = "loading"
        _error = ""

    def _load():
        global _state, _error, _model, _tok
        missing = _missing_deps()
        if missing:
            _state, _error = "absent", f"{missing} not installed"
            return
        try:
            import torch
            from transformers import AutoModel, AutoTokenizer

            # Threads are capped deliberately. Six is what this box reports and
            # letting torch discover its own can oversubscribe badly on a
            # machine already running audio and a GUI.
            try:
                torch.set_num_threads(min(6, torch.get_num_threads()))
            except Exception:
                pass

            _tok = AutoTokenizer.from_pretrained(MODEL_ID)
            _model = AutoModel.from_pretrained(MODEL_ID)
            _model.eval()

            # Prove it works rather than assuming. A truncated or mismatched
            # download raises here instead of silently returning garbage vectors
            # for the whole index.
            with torch.no_grad():
                probe = _tok(["ok"], padding=True, truncation=True,
                             max_length=MAX_TOKENS, return_tensors="pt")
                out = _model(**probe)
                if out.last_hidden_state.shape[-1] != DIMS:
                    raise RuntimeError(
                        f"model produced {out.last_hidden_state.shape[-1]} dims, "
                        f"expected {DIMS}")
            _state = "ready"
            log(f"[Embed] Local model ready ({SPACE}). No API key, no quota.")
        except Exception as e:
            _state = "failed"
            _error = f"{type(e).__name__}: {str(e)[:180]}"
            _model = _tok = None
            log(f"[Embed] Local model unavailable — {_error}")

    threading.Thread(target=_load, name="local-embed-warm", daemon=True).start()


def _mean_pool(out, mask):
    """Attention-masked mean pooling, then L2 normalise.

    Mean pooling rather than CLS because MiniLM was trained that way; using the
    wrong one produces vectors that score, but score meaninglessly.
    """
    import torch
    hidden = out.last_hidden_state
    m = mask.unsqueeze(-1).to(hidden.dtype)
    summed = (hidden * m).sum(dim=1)
    counts = m.sum(dim=1).clamp(min=1e-9)
    return torch.nn.functional.normalize(summed / counts, p=2, dim=1)


def embed(texts: list[str], log=print) -> list[list[float]] | None:
    """Embed `texts`, or return None if this backend is not usable right now.

    None rather than a list of Nones: there is no partial outcome here. Either the
    model is loaded and every text goes through, or nothing does and the caller
    should use another backend. A half-embedded corpus in a single space is worse
    than a corpus that is honestly unembedded.
    """
    if not texts:
        return []
    if not ready():
        return None
    global _fails, _state, _error, _model, _tok
    try:
        import torch
        out_all: list[list[float]] = []
        for i in range(0, len(texts), BATCH):
            batch = texts[i:i + BATCH]
            enc = _tok(batch, padding=True, truncation=True,
                       max_length=MAX_TOKENS, return_tensors="pt")
            with torch.no_grad():
                res = _model(**enc)
            vecs = _mean_pool(res, enc["attention_mask"])
            out_all.extend(v.tolist() for v in vecs)
        _fails = 0
        return out_all
    except Exception as e:
        _fails += 1
        msg = f"{type(e).__name__}: {str(e)[:140]}"
        log(f"[Embed] Local embed failed ({_fails}/{MAX_FAILURES}): {msg}")
        if _fails >= MAX_FAILURES:
            # Stop claiming to be ready. ready() is what the rest of the app asks,
            # and a backend that reports ready and then fails every query is
            # worse than one that admits it is out.
            _state = "failed"
            _error = msg
            _model = _tok = None
            log("[Embed] Local backend disabled for this session. "
                "Keyword search unaffected; restart or switch backend to retry.")
        return None


def embed_query(text: str) -> list[float] | None:
    """Convenience for the search path."""
    if not ready():
        return None
    r = embed([text])
    return r[0] if r else None
