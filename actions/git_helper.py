"""
git_helper — version control by voice: status, diff stat, log, commit, push.

Read-only modes (status, diff, log) run free. commit and push rewrite shared
history-adjacent state, so commit parks behind the on-screen CONFIRM gate
(push rides along only when the user confirms the same banner — one press,
both steps, in that order).
"""

import subprocess

try:
    from core import confirm as _confirm
except Exception:
    _confirm = None

_DEFAULT_TIMEOUT = 30


def _run_git(repo: str, *args: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True, text=True, timeout=_DEFAULT_TIMEOUT)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out.strip()[:3000] or "(no output)"
    except FileNotFoundError:
        return False, "git is not installed."
    except Exception as e:
        return False, f"git failed: {e}"


def _do_commit_push(repo: str, message: str, push: bool, player=None) -> str:
    ok, out = _run_git(repo, "add", "-A")
    if not ok:
        return f"Could not stage: {out}"
    ok, out = _run_git(repo, "commit", "-m", message)
    if not ok:
        return f"Could not commit ({out[:200]})."
    result = f"Committed: {message[:80]}"
    if push:
        ok, out = _run_git(repo, "push")
        result += f" Pushed. {out[:200]}" if ok else f" Push failed: {out[:200]}"
    if player:
        try:
            player.write_log(f"[git] {result}")
        except Exception:
            pass
    return result


def git_helper(parameters=None, response=None, player=None,
               session_memory=None) -> str:
    params = parameters or {}
    mode = str(params.get("mode", "status")).strip().lower()
    repo = str(params.get("repo", "")).strip() or "."
    message = str(params.get("message", "")).strip()

    if mode == "status":
        ok, out = _run_git(repo, "status", "--short", "--branch")
        return ("Working tree clean." if ok and not out
                else (out if ok else f"git status failed: {out}"))
    if mode == "diff":
        ok, out = _run_git(repo, "diff", "--stat")
        if not ok:
            return f"git diff failed: {out}"
        return "No unstaged changes." if not out else out
    if mode == "log":
        ok, out = _run_git(repo, "log", "--oneline", "-8")
        return out if ok else f"git log failed: {out}"
    if mode in ("commit", "push"):
        if not message:
            return "What commit message should I use?"
        if _confirm is not None:
            if _confirm.pending_title():
                return ("There is already a confirmation waiting on screen. "
                        "Ask the user to answer that one first.")
            detail = f"{repo}\n{message[:120]}" + \
                (" + push" if mode == "push" else "")
            return _confirm.request(
                key="git-commit", title="Commit changes", detail=detail,
                run=lambda: _do_commit_push(repo, message,
                                            mode == "push", player))
        return _do_commit_push(repo, message, mode == "push", player)
    return f"Unknown git mode '{mode}'. Use status, diff, log, commit, push."


TOOL = {
    "name": "git_helper",
    "description": (
        "Git version control for a local repo: status, diff stat, recent log, "
        "commit (asks on-screen CONFIRM first), push. Use when the user asks "
        "what changed, to commit work, or to push. commit/push never happen "
        "until the user presses CONFIRM — never claim they are done."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {"type": "STRING",
                     "description": "status | diff | log | commit | push"},
            "repo": {"type": "STRING",
                     "description": "Repo path (default: current project)"},
            "message": {"type": "STRING",
                        "description": "Commit message for commit/push"},
        },
        "required": ["mode"],
    },
    "handler": git_helper,
}
