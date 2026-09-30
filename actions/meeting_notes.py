"""
meeting_notes — "take notes" mode: timestamped bullets into notes/YYYY-MM-DD.md.
"""

from core import notes as _notes


def meeting_notes(parameters=None, response=None, player=None,
                  session_memory=None) -> str:
    params = parameters or {}
    mode = str(params.get("mode", "add")).strip().lower()
    text = str(params.get("text", "")).strip()

    if mode == "start":
        res = _notes.start(text)
    elif mode == "stop":
        res = _notes.stop()
    elif mode == "status":
        st = _notes.status()
        res = (f"Notes active since {st['since']} ({st['file']})."
               if st["active"] else "Notes are not running.")
    else:  # add
        if not text:
            return "What should I note down?"
        res = _notes.add(text)
    if player:
        try:
            player.write_log(f"[notes] {res}")
        except Exception:
            pass
    return res


TOOL = {
    "name": "meeting_notes",
    "description": (
        "Timestamped meeting notes in a daily markdown file. Modes: start "
        "(optional title in text), add (one bullet from text), stop, status. "
        "Use when the user says take notes, note this down, or stop notes."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {"type": "STRING",
                     "description": "start | add | stop | status"},
            "text": {"type": "STRING",
                     "description": "Title for start, bullet for add"},
        },
        "required": ["mode"],
    },
    "handler": meeting_notes,
}
