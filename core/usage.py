"""
core/usage.py — what the assistant spends.

Every successful Gemini text call records its tier and rough character
counts into memory/usage.json (local only, git-ignored), bucketed by day.
The dashboard serves the tail over GET /api/usage, and check_cap() lets
heavy background jobs bow out politely when the user sets a daily call cap
(`daily_call_cap` in config/api_keys.json, 0 = unlimited).

Counts are estimates (characters, not tokens) — good enough to see which
tier burns the budget and to warn before a runaway night does it again.
Never raises out of any function here; metering must never break the call
it measures.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_PATH = _BASE / "memory" / "usage.json"
_MAX_DAYS = 90

_lock = threading.Lock()
_warned_for: str | None = None


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data: dict) -> None:
    try:
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(_PATH)
    except Exception as e:
        print(f"[Usage] warn: could not save: {e}")


def record(tier: str, in_chars: int = 0, out_chars: int = 0) -> None:
    """Log one successful call. Never raises."""
    try:
        with _lock:
            data = _load()
            day = data.setdefault(_today(), {"calls": 0, "tiers": {}})
            day["calls"] = int(day.get("calls", 0)) + 1
            t = day.setdefault("tiers", {}).setdefault(
                str(tier or "?"), {"calls": 0, "in": 0, "out": 0})
            t["calls"] = int(t.get("calls", 0)) + 1
            t["in"] = int(t.get("in", 0)) + max(0, int(in_chars or 0))
            t["out"] = int(t.get("out", 0)) + max(0, int(out_chars or 0))
            for old in sorted(data)[:max(0, len(data) - _MAX_DAYS)]:
                data.pop(old, None)
            _save(data)
    except Exception:
        pass


def summary(days: int = 7) -> dict:
    """Last N days, newest first. Never raises."""
    try:
        days = max(1, min(int(days or 7), _MAX_DAYS))
    except Exception:
        days = 7
    try:
        with _lock:
            data = _load()
        out = [{"date": d, **data[d]} for d in sorted(data, reverse=True)[:days]]
        return {"ok": True, "days": out}
    except Exception:
        return {"ok": True, "days": []}


def _cap() -> int:
    try:
        cfg = json.loads((_BASE / "config" / "api_keys.json")
                         .read_text(encoding="utf-8"))
        return max(0, int(cfg.get("daily_call_cap", 0) or 0))
    except Exception:
        return 0


def check_cap() -> tuple[bool, str]:
    """(True, '') when under the daily cap. Warns once per day when over —
    background jobs should skip; interactive calls still go through (a cap
    that silences the assistant mid-sentence is worse than no cap)."""
    global _warned_for
    try:
        cap = _cap()
        if cap <= 0:
            return True, ""
        with _lock:
            used = int(_load().get(_today(), {}).get("calls", 0))
        if used < cap:
            return True, ""
        if _warned_for != _today():
            _warned_for = _today()
            return False, (f"Daily call cap reached ({used}/{cap}). "
                           "Background jobs paused until tomorrow.")
        return False, ""
    except Exception:
        return True, ""
