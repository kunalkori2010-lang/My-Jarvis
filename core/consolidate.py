"""
core/consolidate.py — memory that files itself.

The model only saves what it happens to call save_memory for mid-conversation,
so durable facts routinely slip through. Once a day this job re-reads recent
conversation history, asks Gemini for the facts worth keeping, and stages
them in memory/staged_facts.json — NOT straight into long-term memory. The
user approves each one from the dashboard (GET/POST /api/staged); nothing is
remembered behind anyone's back.

Guards: skips when the daily call cap is hit, when there is no new history
since the last run, and when the model returns nothing parseable. Never
raises out of any function.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_STAGED = _BASE / "memory" / "staged_facts.json"
_STATE = _BASE / "memory" / "consolidate_state.json"

_CATS = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_lock = threading.Lock()


def _read_json(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)
    except Exception as e:
        print(f"[Consolidate] warn: could not save {path.name}: {e}")


def list_staged() -> list:
    with _lock:
        items = _read_json(_STAGED, [])
        return [i for i in items if isinstance(i, dict)
                and i.get("status") == "staged"]


def _known_keys() -> set:
    try:
        from memory.memory_manager import load_memory
        mem = load_memory()
        out = set()
        for cat, entries in (mem or {}).items():
            if isinstance(entries, dict):
                for k in entries:
                    out.add(f"{cat}/{k}")
        with _lock:
            for i in _read_json(_STAGED, []):
                if isinstance(i, dict):
                    out.add(f"{i.get('category')}/{i.get('key')}")
        return out
    except Exception:
        return set()


def run_once() -> dict:
    """One consolidation pass. Returns {'staged': n} or {'skipped': reason}."""
    try:
        from core import usage as _usage
        ok, msg = _usage.check_cap()
        if not ok:
            return {"skipped": msg or "over daily call cap"}
    except Exception:
        pass
    try:
        from memory import history as _hist
        turns = list(reversed(_hist.recent(40)))
    except Exception:
        return {"skipped": "no history available"}
    if not turns:
        return {"skipped": "no conversation history yet"}
    try:
        state = _read_json(_STATE, {})
        last_t = float(state.get("last_turn_t", 0) or 0)
    except Exception:
        last_t = 0
    fresh = [t for t in turns if float(t.get("t", 0) or 0) > last_t]
    if not fresh:
        return {"skipped": "no new conversations since last run"}
    convo = "\n".join(
        f"User: {t.get('user', '')}\nJARVIS: {t.get('jarvis', '')}".strip()
        for t in fresh[-30:])[:12000]
    prompt = (
        "From this assistant conversation, extract durable personal facts "
        "worth remembering long-term (name, city, job, preferences, hobbies, "
        "relationships, projects, plans). Ignore one-off commands, weather, "
        "and searches. Reply with ONLY a JSON array, each item "
        '{"category": "identity|preferences|projects|relationships|wishes|notes", '
        '"key": "snake_case", "value": "concise English"}. '
        "Empty array if nothing is worth keeping.\n\n" + convo)
    try:
        from core import gemini
        from core.gemini import SMART
        facts = gemini.as_json(prompt, SMART, None, 60_000, default=[])
    except Exception as e:
        return {"skipped": f"model call failed: {e}"}
    if not isinstance(facts, list):
        return {"skipped": "model returned nothing usable"}
    known = _known_keys()
    staged = []
    with _lock:
        items = _read_json(_STAGED, [])
        for f in facts:
            try:
                if not isinstance(f, dict):
                    continue
                cat = str(f.get("category", "")).strip().lower()
                key = str(f.get("key", "")).strip().lower()
                val = str(f.get("value", "")).strip()
                if cat not in _CATS or not _KEY_RE.match(key) or not val:
                    continue
                if f"{cat}/{key}" in known:
                    continue
                known.add(f"{cat}/{key}")
                staged.append({"id": uuid.uuid4().hex[:8], "category": cat,
                               "key": key, "value": val[:300],
                               "status": "staged",
                               "created": datetime.now().isoformat(timespec="seconds")})
            except Exception:
                continue
        items.extend(staged)
        _write_json(_STAGED, items[-200:])
    try:
        newest = max(float(t.get("t", 0) or 0) for t in fresh)
        _write_json(_STATE, {"last_turn_t": newest,
                             "last_run": datetime.now().isoformat(timespec="seconds")})
    except Exception:
        pass
    return {"staged": len(staged)}


def approve(fid: str) -> tuple[bool, str]:
    """Move a staged fact into long-term memory."""
    with _lock:
        items = _read_json(_STAGED, [])
        for i in items:
            if isinstance(i, dict) and i.get("id") == fid \
                    and i.get("status") == "staged":
                try:
                    from memory.memory_manager import remember
                    remember(i["key"], i["value"], i["category"])
                except Exception as e:
                    return False, f"Could not save: {e}"
                i["status"] = "approved"
                _write_json(_STAGED, items)
                return True, f"Remembered {i['category']}/{i['key']}."
        return False, "Fact not found."


def reject(fid: str) -> tuple[bool, str]:
    with _lock:
        items = _read_json(_STAGED, [])
        for i in items:
            if isinstance(i, dict) and i.get("id") == fid \
                    and i.get("status") == "staged":
                i["status"] = "rejected"
                _write_json(_STAGED, items)
                return True, "Discarded."
        return False, "Fact not found."
