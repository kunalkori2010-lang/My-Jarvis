"""
core/routines.py — user-defined automations ("wake me at 7", "brief me at 8").

One JSON file, three schedule kinds, no new dependencies:

  {"kind": "daily",    "time": "08:00"}          — every day at 08:00 local
  {"kind": "interval", "minutes": 60}            — every N minutes (N >= 5)
  {"kind": "once",     "at": "2026-10-02T08:00"} — a single future firing
  {"kind": "macro",    "match": "exact|contains", "text": "studio mode"}
                                                 — phrase trigger (see below)

Macros never fire on a timer: match_macro(text) is checked against every
typed or dashboard command in main.py, and a hit rewrites the turn into the
macro's expansion so Gemini executes the steps with its normal tools.

Routines are the user's explicit instructions, so firing one never consumes
the proactive-initiative budget and is never "dismissal-learned" away — but
it still respects sleep, mute and an active conversation, exactly like the
background monitor does. The main loop (see _run_routines in main.py) polls
due() once a minute and injects the routine's command text into the session.

Storage is memory/routines.json (local only — git-ignored like the rest of
memory/). Writes are atomic. Everything here is sync and never raises out.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "memory" / "routines.json"
_lock = threading.Lock()


def _load() -> list:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(routines: list) -> None:
    try:
        tmp = _PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(routines, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(_PATH)
    except Exception as e:
        print(f"[Routines] warn: could not save: {e}")


def _parse_time_hhmm(raw: str):
    try:
        return datetime.strptime(raw.strip(), "%H:%M").time()
    except Exception:
        return None


def validate_schedule(sched: dict) -> tuple[bool, str]:
    """(ok, normalised-or-error). Never raises."""
    try:
        sched = dict(sched or {})
        kind = str(sched.get("kind", "")).strip().lower()
        if kind == "daily":
            t = _parse_time_hhmm(str(sched.get("time", "")))
            if t is None:
                return False, "daily needs time as HH:MM (e.g. 08:00)."
            return True, {"kind": "daily", "time": t.strftime("%H:%M")}
        if kind == "interval":
            mins = int(sched.get("minutes", 0))
            if mins < 5:
                return False, "interval needs minutes >= 5."
            return True, {"kind": "interval", "minutes": mins}
        if kind == "once":
            at = datetime.fromisoformat(str(sched.get("at", "")))
            if at <= datetime.now():
                return False, "once needs a future datetime."
            return True, {"kind": "once", "at": at.isoformat(timespec="minutes")}
        if kind == "macro":
            match = str(sched.get("match", "contains")).strip().lower()
            text = str(sched.get("text", "")).strip().lower()
            if match not in ("exact", "contains"):
                return False, "macro match must be exact or contains."
            if not text:
                return False, "macro needs the trigger text."
            return True, {"kind": "macro", "match": match, "text": text}
        return False, "kind must be daily, interval, once, or macro."
    except Exception as e:
        return False, f"bad schedule: {e}"


def add(name: str, schedule: dict, command: str) -> dict:
    """Add a routine. Returns the routine or {'error': ...}."""
    name, command = (name or "").strip(), (command or "").strip()
    if not name:
        return {"error": "A routine needs a name."}
    if not command:
        return {"error": "A routine needs a command to run."}
    ok, sched = validate_schedule(schedule)
    if not ok:
        return {"error": sched}
    with _lock:
        routines = _load()
        r = {"id": uuid.uuid4().hex[:8], "name": name,
             "schedule": sched, "command": command,
             "enabled": True, "last_run": None,
             "created": datetime.now().isoformat(timespec="seconds")}
        routines.append(r)
        _save(routines)
        return r


def list_all() -> list:
    with _lock:
        return _load()


def remove(rid: str) -> bool:
    with _lock:
        routines = _load()
        kept = [r for r in routines if r.get("id") != rid]
        if len(kept) == len(routines):
            return False
        _save(kept)
        return True


def set_enabled(rid: str, enabled: bool) -> bool:
    with _lock:
        routines = _load()
        hit = False
        for r in routines:
            if r.get("id") == rid:
                r["enabled"] = bool(enabled)
                hit = True
        if hit:
            _save(routines)
        return hit


def match_macro(text: str) -> dict | None:
    """First enabled macro whose trigger fits `text`, else None. Macros are
    checked before the turn reaches the model (see _on_text_command)."""
    t = (text or "").strip().lower()
    if not t:
        return None
    with _lock:
        for r in _load():
            if not r.get("enabled"):
                continue
            s = r.get("schedule", {})
            if s.get("kind") != "macro":
                continue
            trig = str(s.get("text", ""))
            if not trig:
                continue
            if s.get("match") == "exact" and t == trig:
                return dict(r)
            if s.get("match", "contains") == "contains" and trig in t:
                return dict(r)
    return None


def due(now: datetime | None = None) -> list:
    """Routines whose schedule fires at `now`. Marks runs; one-shot
    routines are consumed (removed) when they fire."""
    now = now or datetime.now()
    fired = []
    with _lock:
        routines = _load()
        keep = []
        changed = False
        for r in routines:
            if not r.get("enabled"):
                keep.append(r)
                continue
            s = r.get("schedule", {})
            kind = s.get("kind")
            fire = False
            if kind == "daily":
                t = _parse_time_hhmm(str(s.get("time", "")))
                if t is not None:
                    last = r.get("last_run")
                    today_done = False
                    if last:
                        try:
                            today_done = datetime.fromisoformat(last).date() == now.date()
                        except Exception:
                            today_done = False
                    if not today_done and now.time() >= t:
                        fire = True
            elif kind == "interval":
                try:
                    mins = max(5, int(s.get("minutes", 0)))
                except Exception:
                    mins = 0
                if mins:
                    last = r.get("last_run")
                    if not last:
                        fire = True
                    else:
                        try:
                            fire = (now - datetime.fromisoformat(last)
                                    >= timedelta(minutes=mins))
                        except Exception:
                            fire = False
            elif kind == "once":
                try:
                    if datetime.fromisoformat(str(s.get("at"))) <= now:
                        fire = True
                except Exception:
                    pass
            if fire:
                r["last_run"] = now.isoformat(timespec="seconds")
                fired.append(dict(r))
                changed = True
                if kind != "once":
                    keep.append(r)
                # once → dropped (consumed)
            else:
                keep.append(r)
        if changed:
            _save(keep)
    return fired
