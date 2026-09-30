"""
Knowledge base — searching the user's OWN files.

The gap this fills: `file_processor` can only act on a file the user explicitly
names or drops on the interface. Nothing in the app could answer "what did I
write about the Sharma contract?" because no code ever looked at the Documents
folder. This is the action the model reaches for; the indexing side lives in
actions/knowledge_index.py because the loader takes one TOOL per file.
"""
from core import knowledge


def _log(message: str, player=None) -> None:
    print(f"[Knowledge] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


def search_action(parameters: dict, player=None, session_memory=None) -> str:
    query = (parameters.get("query") or "").strip()
    if not query:
        return "I need something to search for — what should I look up?"

    try:
        limit = int(parameters.get("max_results") or 6)
    except (TypeError, ValueError):
        limit = 6
    limit = max(1, min(limit, 12))

    try:
        hits = knowledge.search(query, limit=limit)
    except Exception as e:
        _log(f"search failed: {e}", player)
        return f"The knowledge search failed: {e}"

    if not hits:
        return (f"I found nothing about '{query}' in your indexed files. "
                "If you haven't indexed them yet, ask me to index your files "
                "and then I'll try again.")

    if hits[0].get("via") == "empty":
        return hits[0]["text"]

    out = [f"Found {len(hits)} matching passage(s) for '{query}':"]
    for i, h in enumerate(hits, 1):
        body = " ".join(h["text"].split())
        if len(body) > 900:
            body = body[:900].rsplit(" ", 1)[0] + "…"
        out.append(f"\n[{i}] {h['path']}  (matched by {h['via']})\n{body}")
    return "\n".join(out)


TOOL = {
    "name": "search_my_files",
    "description": (
        "Search the user's OWN indexed local files (Documents, Desktop, "
        "Downloads, and any folder added to the index) and return the matching "
        "passages. Use this whenever the user refers to something they wrote, "
        "saved, downloaded or worked on — notes, reports, contracts, code, "
        "spreadsheets, PDFs — or asks what they said about a topic, a person or "
        "a project. Call this BEFORE answering from general knowledge when the "
        "question is about the user's own material. If the index is empty, call "
        "index_my_files first, then search again."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": ("What to look for, in the user's own words. "
                                "Natural language works best — 'the budget "
                                "section of my internship report'."),
            },
            "max_results": {
                "type": "INTEGER",
                "description": ("How many passages to return (1-12, default 6). "
                                "Use more when the question is broad."),
            },
        },
        "required": ["query"],
    },
    "handler": search_action,
}
