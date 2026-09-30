"""
disk_janitor — honest disk cleanup.

Two modes, and the default is the safe one:
  scan  — report reclaimable space (old Downloads, temp dir), change nothing.
  clean — move Downloads files older than N days to the OS trash
          (recoverable from the Recycle Bin / Trash, never hard-deleted).

Trashing is one-way through the Python API (no restore call exists), so
clean always lists exactly what it moved and says where to undo it. When
send2trash is missing, clean refuses rather than falling back to deletion.
"""

import os
import tempfile
import time
from pathlib import Path

try:
    from send2trash import send2trash as _trash
except ImportError:
    _trash = None


def _dir_size(root: Path, older_than_days: int = 0) -> tuple[int, int, list]:
    total, count, old = 0, 0, []
    try:
        now = time.time()
        for p in root.rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    st = p.stat()
                    total += st.st_size
                    count += 1
                    if older_than_days and now - st.st_mtime > older_than_days * 86400:
                        old.append(p)
            except Exception:
                continue
    except Exception:
        pass
    return total, count, old


def _fmt(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GB"


def disk_janitor(parameters=None, response=None, player=None,
                 session_memory=None) -> str:
    params = parameters or {}
    mode = str(params.get("mode", "scan")).strip().lower()
    try:
        days = max(1, min(int(params.get("days", 30)), 365))
    except Exception:
        days = 30

    downloads = Path.home() / "Downloads"
    tmp = Path(tempfile.gettempdir())

    if mode == "scan":
        lines = []
        if downloads.exists():
            size, n, old = _dir_size(downloads, days)
            lines.append(f"Downloads: {_fmt(size)} in {n} files "
                         f"({_fmt(sum(p.stat().st_size for p in old[:1000] if p.exists()))} older than {days}d)")
        try:
            tsize, tn, _ = _dir_size(tmp)
            lines.append(f"Temp: {_fmt(tsize)} in {tn} files (left alone — apps may hold them open)")
        except Exception:
            pass
        return "Disk scan:\n" + ("\n".join(lines) if lines else "nothing found.")

    if mode == "clean":
        if _trash is None:
            return ("send2trash is not installed — I refuse to delete without "
                    "a trash can. Run: pip install send2trash")
        if not downloads.exists():
            return "No Downloads folder found."
        _, _, old = _dir_size(downloads, days)
        moved, failed, freed = 0, 0, 0
        for p in old[:500]:
            try:
                freed += p.stat().st_size
                _trash(str(p))
                moved += 1
            except Exception:
                failed += 1
        msg = (f"Moved {moved} files ({_fmt(freed)}) older than {days}d to "
               f"the trash{f' ({failed} failed)' if failed else ''}. "
               "Undo from the Recycle Bin / Trash.")
        print(f"[Janitor] {msg}")
        if player:
            try:
                player.write_log(f"[janitor] {msg}")
            except Exception:
                pass
        return msg

    return f"Unknown janitor mode '{mode}'. Use scan or clean."


TOOL = {
    "name": "disk_janitor",
    "description": (
        "Disk cleanup. scan reports reclaimable space (old Downloads, temp) "
        "and changes nothing. clean moves Downloads files older than N days "
        "to the OS trash (recoverable). Always scan first."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {"type": "STRING",
                     "description": "scan (default) | clean"},
            "days": {"type": "STRING",
                     "description": "Age threshold in days for clean (default 30)"},
        },
        "required": ["mode"],
    },
    "handler": disk_janitor,
}
