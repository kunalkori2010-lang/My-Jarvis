"""
focus_timer — voice control for focus sessions (see core/focus.py).
"""

from core import focus as _focus


def focus_timer(parameters=None, response=None, player=None,
                session_memory=None) -> str:
    params = parameters or {}
    mode = str(params.get("mode", "status")).strip().lower()

    if mode == "start":
        try:
            minutes = int(params.get("minutes", 50))
        except Exception:
            minutes = 50
        res = _focus.start(minutes, str(params.get("label", "")))
    elif mode == "stop":
        queued, res = _focus.stop()
        if queued:
            res += " Say each briefly."
    elif mode == "status":
        st = _focus.status()
        if st["active"]:
            mins = st["seconds_left"] // 60
            res = (f"Focusing ({st['label'] or 'no label'}) — {mins} min left, "
                   f"{st['queued']} held.")
        else:
            res = "No focus session running."
    else:
        return f"Unknown focus mode '{mode}'. Use start, stop, status."
    if player:
        try:
            player.write_log(f"[focus] {res}")
        except Exception:
            pass
    return res


TOOL = {
    "name": "focus_timer",
    "description": (
        "Focus sessions: start (minutes + optional label) mutes all "
        "unprompted speech until the timer ends, stop ends early and reports "
        "what arrived, status checks the timer. Use when the user says focus, "
        "do not disturb, or asks what came in."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {"type": "STRING",
                     "description": "start | stop | status"},
            "minutes": {"type": "STRING",
                        "description": "Length for start (default 50)"},
            "label": {"type": "STRING",
                      "description": "Optional label, e.g. 'deep work'"},
        },
        "required": ["mode"],
    },
    "handler": focus_timer,
}
