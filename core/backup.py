"""
core/backup.py — one-file safety net for everything JARVIS knows about you.

export() zips the personal-data files into memory/backups/jarvis-backup-<date>.zip:
  memory/long_term.json, conversation_history.json, routines.json,
  staged_facts.json, usage.json, checklists.json, focus_state.json (if present),
  notes/*.md, plus a manifest. Secrets are NEVER included: no api_keys.json,
  no OAuth tokens, no client secrets, no TLS certs, no voiceprint.

restore(path) validates the manifest + member list before touching anything,
writes through temp files, and refuses archives with absolute paths or "..".
After a restore, restart JARVIS so every engine re-reads its files.

Never raises out of any function — returns (ok, message).
"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_MEM = _BASE / "memory"
_NOTES = _BASE / "notes"
_BACKUP_DIR = _MEM / "backups"

_MEMBERS = ["long_term.json", "conversation_history.json", "routines.json",
            "staged_facts.json", "usage.json", "checklists.json",
            "focus_state.json", "affect_state.json", "initiative_state.json"]
_MAX_BYTES = 50 * 1024 * 1024


def _manifest() -> dict:
    return {"app": "jarvis", "v": 1,
            "at": datetime.now().isoformat(timespec="seconds")}


def export() -> tuple[bool, str]:
    """Write a dated backup zip. Returns (True, path) / (False, reason)."""
    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        dest = _BACKUP_DIR / ("jarvis-backup-"
                              + datetime.now().strftime("%Y%m%d-%H%M%S") + ".zip")
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(_manifest(), indent=1))
            for name in _MEMBERS:
                p = _MEM / name
                if p.exists() and p.is_file():
                    z.write(p, f"memory/{name}")
            if _NOTES.exists():
                for md in sorted(_NOTES.glob("*.md")):
                    if md.is_file() and md.stat().st_size < 5 * 1024 * 1024:
                        z.write(md, f"notes/{md.name}")
        return True, str(dest)
    except Exception as e:
        return False, f"Backup failed: {e}"


def _safe_members(z: zipfile.ZipFile):
    infos = z.infolist()
    if len(infos) > 500:
        return None, "too many files"
    total = 0
    for i in infos:
        n = i.filename
        if n.startswith("/") or ".." in n.split("/") or n.startswith("\\"):
            return None, f"unsafe path: {n}"
        if not (n == "manifest.json" or n.startswith("memory/")
                or n.startswith("notes/")):
            return None, f"unexpected file: {n}"
        total += i.file_size
        if total > _MAX_BYTES:
            return None, "archive too large"
    try:
        man = json.loads(z.read("manifest.json").decode("utf-8"))
        if man.get("app") != "jarvis":
            return None, "not a JARVIS backup"
    except Exception:
        return None, "missing/invalid manifest"
    return infos, ""


def restore(data: bytes) -> tuple[bool, str]:
    """Restore from uploaded zip bytes. Returns (ok, message)."""
    try:
        import io
        if not data or len(data) > _MAX_BYTES:
            return False, "Empty file or over 50 MB."
        z = zipfile.ZipFile(io.BytesIO(data))
        infos, err = _safe_members(z)
        if infos is None:
            return False, f"Refused: {err}."
        restored = []
        for i in infos:
            if i.filename == "manifest.json" or i.is_dir():
                continue
            raw = z.read(i.filename)
            if i.filename.startswith("memory/"):
                dest = _MEM / Path(i.filename).name
            else:
                _NOTES.mkdir(parents=True, exist_ok=True)
                dest = _NOTES / Path(i.filename).name
            tmp = dest.with_suffix(dest.suffix + ".tmp")
            tmp.write_bytes(raw)
            tmp.replace(dest)
            restored.append(i.filename)
        return True, (f"Restored {len(restored)} file(s). "
                      "Restart JARVIS so everything re-reads them.")
    except Exception as e:
        return False, f"Restore failed: {e}"


def list_backups() -> list:
    try:
        if not _BACKUP_DIR.exists():
            return []
        return [{"name": p.name, "size": p.stat().st_size,
                 "at": datetime.fromtimestamp(p.stat().st_mtime)
                 .isoformat(timespec="seconds")}
                for p in sorted(_BACKUP_DIR.glob("jarvis-backup-*.zip"),
                                reverse=True)[:20]]
    except Exception:
        return []
