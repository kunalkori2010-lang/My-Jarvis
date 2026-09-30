"""
self_update — "update yourself": git pull the project, then say restart.

Rewriting its own code is as irreversible as it gets for this app, so the
pull parks behind the on-screen CONFIRM gate like shutdown does. New tools
only load at startup, so the result always ends with "restart me".
"""

import subprocess
import sys
from pathlib import Path

try:
    from core import confirm as _confirm
except Exception:
    _confirm = None


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _do_pull(player=None) -> str:
    base = _base_dir()
    try:
        r = subprocess.run(
            ["git", "-C", str(base), "pull", "--rebase"],
            capture_output=True, text=True, timeout=120)
        out = ((r.stdout or "") + (r.stderr or "")).strip()[:800]
        if r.returncode != 0:
            return f"Update failed: {out or 'git error'}"
        if "Already up to date" in out:
            res = "Already up to date — nothing changed."
        else:
            res = f"Updated. {out[:300]} Restart me to use the new code."
        print(f"[Update] {res}")
        if player:
            try:
                player.write_log(f"[update] {res}")
            except Exception:
                pass
        return res
    except FileNotFoundError:
        return "git is not installed."
    except Exception as e:
        return f"Update failed: {e}"


def self_update(parameters=None, response=None, player=None,
                session_memory=None) -> str:
    if _confirm is not None:
        if _confirm.pending_title():
            return ("There is already a confirmation waiting on screen. "
                    "Ask the user to answer that one first.")
        return _confirm.request(
            key="self-update", title="Update JARVIS",
            detail="git pull the project code. Takes effect on restart.",
            run=lambda: _do_pull(player))
    return _do_pull(player)


TOOL = {
    "name": "self_update",
    "description": (
        "Update JARVIS itself with git pull. Asks on-screen CONFIRM first "
        "and needs a restart afterwards. Use when the user says update "
        "yourself or check for updates. Never claim it is done before "
        "confirmation."),
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": self_update,
}
