"""
core/focus.py — "focus for 50 minutes" with teeth.

While a focus session runs, the three unprompted-speech loops (proactive,
background monitor, routines) hold their fire and queue what arrived instead.
When the timer ends, JARVIS reports the queue in one line each — nothing
interrupts, nothing is lost.

State lives in memory/focus_state.json (git-ignored). All sync, never raises.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "memory" / "focus_state.json"
_lock = threading.Lock()
_MAX_QUEUED = 20


def _blank() -> dict:
    return {"active": False, "until": 0.0, "label": "", "queued": []}


def _load() -> dict:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        st = _blank()
        if isinstance(data, dict):
            st.update({k: data.get(k, v) for k, v in _blank().items()})
        if st["active"] and time.time() >= float(st["until"] or 0):
            st["active"] = False  # timer lapsed while we were away
        return st
    except Exception:
        return _blank()


def _save(st: dict) -> None:
    try:
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(_PATH)
    except Exception:
        pass


def start(minutes: int, label: str = "") -> str:
    try:
        minutes = max(1, min(int(minutes), 480))
    except Exception:
        return "Focus for how many minutes?"
    with _lock:
        st = _load()
        st.update({"active": True,
                   "until": time.time() + minutes * 60,
                   "label": (label or "").strip()[:80],
                   "queued": []})
        _save(st)
    what = f" on {label.strip()}" if label.strip() else ""
    return f"Focusing for {minutes} minutes{what}. I'll hold everything until then."


def stop() -> tuple[list, str]:
    """End early. Returns (queued messages, summary)."""
    with _lock:
        st = _load()
        queued = list(st.get("queued", []))[-_MAX_QUEUED:]
        st.update({"active": False, "until": 0.0, "queued": []})
        _save(st)
    if not queued:
        return [], "Focus over. Nothing arrived."
    return queued, (f"Focus over. While you were away: "
                    + "; ".join(str(q)[:120] for q in queued))


def status() -> dict:
    with _lock:
        st = _load()
    left = max(0, int(st["until"] - time.time())) if st["active"] else 0
    return {"active": st["active"], "seconds_left": left,
            "label": st.get("label", ""), "queued": len(st.get("queued", []))}


def active() -> bool:
    try:
        return bool(_load()["active"])
    except Exception:
        return False


def hold(message: str) -> bool:
    """Park one interruption for later. True when focus ate it."""
    message = (message or "").strip()
    if not message or not active():
        return False
    with _lock:
        st = _load()
        if not st["active"]:
            return False
        q = st.get("queued", [])
        q.append({"t": datetime.now().isoformat(timespec="seconds"),
                  "m": message[:200]})
        st["queued"] = q[-_MAX_QUEUED:]
        _save(st)
    return True
