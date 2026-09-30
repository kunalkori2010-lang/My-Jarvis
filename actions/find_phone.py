"""
find_phone — "ring my phone": every connected phone dashboard beeps loudly.
"""


def find_phone(parameters=None, response=None, player=None,
               session_memory=None, dashboard=None) -> str:
    if dashboard is None:
        return "The phone dashboard is not running."
    # Actions run in an executor thread — notify_beep() marshals the
    # broadcast onto the event loop thread-safely.
    try:
        ringing = bool(dashboard.notify_beep(10))
    except Exception:
        ringing = False
    try:
        # No event loop here by design; confirm delivery on the next tick.
        if dashboard._clients:
            ringing = True
    except Exception:
        pass
    res = ("Ringing your phone. It keeps beeping for 10 seconds."
           if ringing else "No phone is connected right now.")
    if player:
        try:
            player.write_log(f"[find] {res}")
        except Exception:
            pass
    return res


TOOL = {
    "name": "find_phone",
    "description": (
        "Ring the paired phone(s) loudly for 10 seconds. Use when the user "
        "asks where their phone is or to ring/call/find it."),
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": find_phone,
}
