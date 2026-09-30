"""
core/notes.py — timestamped meeting notes in daily markdown files.

"Take notes" opens today's notes/YYYY-MM-DD.md and every line until "stop
notes" gets a timestamp. Nothing clever, nothing to configure — the value is
that it never forgets to write things down. Files live in notes/ (git-ignored:
they are your words, not source). Never raises out of any function.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_DIR = _BASE / "notes"

_lock = threading.Lock()
_active_since: str | None = None


def _today_file() -> Path:
    _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR / (datetime.now().strftime("%Y-%m-%d") + ".md")


def start(title: str = "") -> str:
    """Begin (or resume) today's session. Idempotent."""
    global _active_since
    with _lock:
        path = _today_file()
        try:
            if not path.exists():
                path.write_text(f"# Notes — {path.stem}\n\n", encoding="utf-8")
            title = (title or "").strip()
            stamp = datetime.now().strftime("%H:%M")
            with open(path, "a", encoding="utf-8") as f:
                if title:
                    f.write(f"\n## {stamp} — {title[:120]}\n")
                else:
                    f.write(f"\n## {stamp}\n")
            _active_since = datetime.now().isoformat(timespec="seconds")
        except Exception as e:
            return f"Could not open notes: {e}"
        return f"Taking notes in {path.name}."


def add(line: str) -> str:
    """Append one timestamped bullet. Auto-starts the day file if needed."""
    line = (line or "").strip()
    if not line:
        return "Nothing to note."
    with _lock:
        try:
            path = _today_file()
            if not path.exists():
                path.write_text(f"# Notes — {path.stem}\n\n", encoding="utf-8")
            stamp = datetime.now().strftime("%H:%M")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"- [{stamp}] {line[:500]}\n")
        except Exception as e:
            return f"Could not write note: {e}"
        return "Noted."


def stop() -> str:
    """End the session. The file stays — notes are never deleted by stopping."""
    global _active_since
    with _lock:
        if _active_since is None:
            return "Notes weren't running."
        _active_since = None
        return "Notes stopped. The file is kept."


def status() -> dict:
    with _lock:
        return {"active": _active_since is not None,
                "since": _active_since,
                "file": _today_file().name}
