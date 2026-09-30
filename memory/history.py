"""
memory/history.py — searchable conversation history.

_long_term.json_ is for facts worth remembering; this file is for the raw
exchanges themselves. Every turn with user text, a JARVIS reply, or both is
appended here with a timestamp, newest last, capped so the file can never grow
without bound. The dashboard serves the tail over /api/history and pushes it
to the /hud socket on connect — which is the conversation-history panel the
roadmap promised, without a database or a new dependency.

Writes are atomic (tmp file + rename) so a crash mid-turn can never corrupt
the log. Everything stays in memory/conversation_history.json on this machine.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent
_HISTORY_PATH = _BASE / "conversation_history.json"
_MAX_TURNS = 500

_lock = threading.Lock()


def _read_all() -> list:
    try:
        data = json.loads(_HISTORY_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def append_turn(user_text: str = "", jarvis_text: str = "") -> None:
    """Record one exchange. No-op when both sides are empty."""
    user_text = (user_text or "").strip()
    jarvis_text = (jarvis_text or "").strip()
    if not user_text and not jarvis_text:
        return
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "t": time.time(),
        "user": user_text,
        "jarvis": jarvis_text,
    }
    with _lock:
        try:
            turns = _read_all()
            turns.append(entry)
            turns = turns[-_MAX_TURNS:]
            tmp = _HISTORY_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(turns, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(_HISTORY_PATH)
        except Exception as e:
            print(f"[History] warn: could not save turn: {e}")


def recent(n: int = 20) -> list:
    """Newest-first tail of the history. Never raises."""
    try:
        n = max(1, min(int(n or 20), _MAX_TURNS))
    except Exception:
        n = 20
    with _lock:
        try:
            return list(reversed(_read_all()[-n:]))
        except Exception:
            return []


def search(keyword: str, n: int = 20) -> list:
    """Case-insensitive substring search over both sides. Never raises."""
    kw = (keyword or "").strip().lower()
    if not kw:
        return recent(n)
    with _lock:
        try:
            hits = [t for t in _read_all()
                    if kw in str(t.get("user", "")).lower()
                    or kw in str(t.get("jarvis", "")).lower()]
            return list(reversed(hits[-n:]))
        except Exception:
            return []
