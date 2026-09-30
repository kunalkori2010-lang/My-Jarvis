"""
checklists — shopping / todo / packing lists by voice.
"""

from core import checklists as _lists


def checklists(parameters=None, response=None, player=None,
               session_memory=None) -> str:
    params = parameters or {}
    mode = str(params.get("mode", "show")).strip().lower()
    name = str(params.get("list", "todo")).strip()
    text = str(params.get("text", "")).strip()

    if mode == "lists":
        all_lists = _lists.lists()
        if not all_lists:
            return "No lists yet. Say 'add milk to shopping' to start one."
        return "Lists:\n" + "\n".join(
            f"• {l['name']} ({l['open']} open of {l['total']})"
            for l in all_lists)
    if mode == "show":
        items = _lists.show(name)
        if not items:
            return f"'{name}' is empty."
        lines = [f"{'✓' if i.get('done') else '○'} {i.get('t')}" for i in items]
        return f"{name}:\n" + "\n".join(lines)
    if mode == "add":
        res = _lists.add(name, text)
    elif mode == "check":
        res = _lists.check(name, text)
    elif mode == "clear":
        res = _lists.clear_done(name)
    else:
        return f"Unknown list mode '{mode}'. Use show, add, check, clear, lists."
    if player:
        try:
            player.write_log(f"[lists] {res}")
        except Exception:
            pass
    return res


TOOL = {
    "name": "checklists",
    "description": (
        "Shopping/todo/packing lists. Modes: show (list name in 'list'), add "
        "(item in 'text'), check (match in 'text'), clear (done items), "
        "lists (all). Use when the user mentions a shopping list, todo, or "
        "packing."),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {"type": "STRING",
                     "description": "show | add | check | clear | lists"},
            "list": {"type": "STRING",
                     "description": "List name (default todo)"},
            "text": {"type": "STRING",
                     "description": "Item text for add/check"},
        },
        "required": ["mode"],
    },
    "handler": checklists,
}
