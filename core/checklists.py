"""
core/checklists.py — lists by voice: shopping, todos, packing, anything.

Lists live in memory/checklists.json (git-ignored). One file, plain shape:

  {"groceries": {"items": [{"t": "milk", "done": false, "at": "..."}], ...}}

All sync, atomic writes, never raises out.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "memory" / "checklists.json"
_lock = threading.Lock()
_MAX_ITEMS = 200


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
        print(f"[Lists] warn: could not save: {e}")


def _name(raw: str) -> str:
    return (raw or "").strip().lower()[:60] or "todo"


def lists() -> list:
    with _lock:
        return [{"name": n, "open": sum(1 for i in v.get("items", [])
                                        if not i.get("done")),
                "total": len(v.get("items", []))}
                for n, v in _load().items()]


def show(name: str) -> list:
    with _lock:
        return [dict(i) for i in _load().get(_name(name), {}).get("items", [])]


def add(name: str, text: str) -> str:
    text = (text or "").strip()
    if not text:
        return "Add what to the list?"
    with _lock:
        data = _load()
        lst = data.setdefault(_name(name), {"items": []})
        if len(lst["items"]) >= _MAX_ITEMS:
            return f"'{_name(name)}' is full ({_MAX_ITEMS})."
        lst["items"].append({"t": text[:200], "done": False,
                             "at": datetime.now().isoformat(timespec="seconds")})
        _save(data)
    return f"Added to {_name(name)}: {text[:80]}"


def check(name: str, text: str) -> str:
    text = (text or "").strip().lower()
    if not text:
        return "Check off what?"
    with _lock:
        data = _load()
        lst = data.get(_name(name))
        if not lst:
            return f"No list called '{_name(name)}'."
        for i in lst["items"]:
            if not i.get("done") and text in str(i.get("t", "")).lower():
                i["done"] = True
                _save(data)
                return f"Checked off: {i['t']}"
        return f"Nothing open matching '{text}'."


def clear_done(name: str) -> str:
    with _lock:
        data = _load()
        lst = data.get(_name(name))
        if not lst:
            return f"No list called '{_name(name)}'."
        before = len(lst["items"])
        lst["items"] = [i for i in lst["items"] if not i.get("done")]
        _save(data)
        return f"Cleared {before - len(lst['items'])} done item(s)."


def delete_list(name: str) -> bool:
    with _lock:
        data = _load()
        if _name(name) not in data:
            return False
        del data[_name(name)]
        _save(data)
        return True
