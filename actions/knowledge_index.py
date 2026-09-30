"""
Knowledge base — building the index of the user's own files.

Split from actions/knowledge_search.py because core/action_loader.py takes one
TOOL dict per file. Indexing and searching are also used at completely different
moments: the first happens once (and after big changes), the second constantly,
so a question must never be what triggers a five-minute crawl of the disk.
"""
from pathlib import Path

from core import knowledge


def _log(message: str, player=None) -> None:
    print(f"[Knowledge] {message}")
    if player:
        try:
            player.write_log(f"JARVIS: {message}")
        except Exception:
            pass


def index_action(parameters: dict, player=None, session_memory=None) -> str:
    """Build or refresh the local index, reporting progress as it goes — the
    alternative to a silent multi-minute wait is the user assuming it hung."""
    folders = parameters.get("folders") or ""
    roots = None
    if isinstance(folders, str) and folders.strip():
        roots = [Path(p.strip()) for p in folders.split(",") if p.strip()]
    embed = parameters.get("embed", True)

    # Say this before doing it, not in a footnote afterwards. "Index my files"
    # is a request the user will reasonably hear as "read them locally", and the
    # embedding step does send chunk text to Google.
    if embed:
        notice = ("I'm indexing that now — the index is stored on this machine, "
                  "but building it sends the text of each passage to Google's "
                  "embedding service. Say the word and I'll build a local-only "
                  "index instead, which stays on this machine entirely.")
        print(f"[Knowledge] {notice}")
        if player:
            try:
                player.write_log(f"JARVIS: {notice}")
            except Exception:
                pass

    if player:
        try:
            player.write_log("SYS: Indexing your files — one moment.")
        except Exception:
            pass

    try:
        stats = knowledge.reindex_guarded(roots, embed=embed)
    except Exception as e:
        _log(f"indexing failed: {e}", player)
        return f"Indexing failed: {e}"

    if "error" in stats:
        return f"I couldn't index anything: {stats['error']}"

    lines = [
        f"Indexed {stats['total_files']} files into {stats['total_chunks']} "
        f"passages ({stats['vectors']} searchable semantically).",
        f"This pass: {stats['files_indexed']} new or changed, "
        f"{stats['files_skipped']} skipped, {stats['seconds']}s.",
        f"Folders: {', '.join(stats['roots'])}",
    ]
    if not embed:
        lines.append("Local-only mode: nothing was sent to Google, so search is "
                     "by keyword only.")
    elif not stats["vectors"]:
        lines.append("Note: semantic search is unavailable right now, so I'm "
                     "matching on keywords only until it recovers.")
    return "\n".join(lines)


TOOL = {
    "name": "index_my_files",
    "description": (
        "Build or refresh the local index of the user's files so search_my_files "
        "can answer questions from them. Slow on the first run, cheap after that "
        "because only changed files are re-read. Call this when the user asks you "
        "to remember, index or learn their files/documents/notes, or when a "
        "search reports the index is empty. IMPORTANT — before you run this, tell "
        "the user that the index is stored locally but that the text of each "
        "passage is sent to Google's embedding service to build it, and offer a "
        "local-only alternative (embed=false) that never contacts Google. Do not "
        "index a folder without saying so first."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "folders": {
                "type": "STRING",
                "description": ("Comma-separated absolute folder paths to index. "
                                "Leave empty for the default: Documents, Desktop "
                                "and Downloads. Example: 'C:/Users/me/Projects,"
                                "D:/Archive'."),
            },
            "embed": {
                "type": "BOOLEAN",
                "description": (
                    "True (default) builds semantic search, which sends passage "
                    "text to Google's embedding service. False builds a "
                    "local-only index that never contacts Google and searches by "
                    "keyword. Use false if the user wants nothing uploaded."),
            },
        },
        "required": [],
    },
    "handler": index_action,
    "behavior": "SILENT",   # the log line already told the user it's happening
}
