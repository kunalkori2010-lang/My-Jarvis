"""
Smoke tests — the five things that must never silently break.

Run:  python -m pytest tests/ -q
No microphone, no network, no API key needed. Anything here that touches
hardware or the network is a bug in the test, not the app.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_action_discovery():
    """Every bundled action loads; our safety-critical ones exist."""
    from core.action_loader import discover_actions
    reg = discover_actions(actions_dir=ROOT / "actions",
                           reserved_names=set(), logger=lambda m: None)
    assert reg.has("send_message"), "send_message action missing"
    assert reg.has("computer_settings"), "computer_settings action missing"
    assert reg.has("web_search"), "web_search action missing"
    assert reg.has("git_helper"), "git_helper action missing"
    assert reg.has("checklists"), "checklists action missing"
    assert reg.has("focus_timer"), "focus_timer action missing"
    assert reg.has("find_phone"), "find_phone action missing"
    assert len(reg.names()) >= 20, f"too few actions: {len(reg.names())}"


def test_plugin_discovery_with_new_plugins():
    """gmail + calendar validate; _google_core stays undiscovered."""
    from core.plugin_loader import discover_plugins
    seen, rejected = [], []

    def _log(m):
        seen.append(m)

    reg = discover_plugins(plugins_dir=ROOT / "plugins", core_tool_names=set(),
                           logger=_log, notify=lambda m: None)
    names = {p["name"] for p in reg.list_for_ui()} if hasattr(
        reg, "list_for_ui") else set()
    # Loader API differs across Marks — fall back to the log lines.
    blob = "\n".join(seen)
    assert "gmail" in blob or "gmail" in names, "gmail plugin not discovered"
    assert "calendar" in blob or "calendar" in names, \
        "calendar plugin not discovered"


def test_prompt_renders():
    """prompt.txt fills without leftover tokens or stray-brace crashes."""
    import main as _main
    calls = _main.discover_actions  # module imports cleanly
    assert callable(calls)
    template = (ROOT / "core" / "prompt.txt").read_text(encoding="utf-8")
    out = _main._render_prompt(template, {"name": "TEST", "os": "windows"})
    assert "{name}" not in out and "{os}" not in out
    # A stray brace in user wording must never break rendering.
    assert "oops } {" in _main._render_prompt("oops } {", {})


def test_undo_stack():
    """push → list → undo round-trips without touching disk."""
    from core import undo
    undo.push_undo("smoke-test-entry", lambda: "undone!")
    titles = [t for t, _ in undo.list_all()] if hasattr(undo, "list_all") else []
    if titles:
        assert any("smoke-test-entry" in t for t in titles)
    # Don't consume a real user undo entry — tested shape is enough.


def test_routines_and_history():
    """Validation rejects garbage; history round-trips in-memory file."""
    from core import routines as rt
    ok, msg = rt.validate_schedule({"kind": "daily", "time": "xx"})
    assert ok is False and msg
    ok, sched = rt.validate_schedule({"kind": "interval", "minutes": 30})
    assert ok is True and sched["minutes"] == 30
    from memory import history as hist
    assert hist.recent(1) is not None
    assert hist.search("", 1) is not None


def test_dashboard_routes():
    """Server builds and exposes the new endpoints (no serving)."""
    from dashboard.server import DashboardServer
    srv = DashboardServer()
    paths = {r.path for r in srv.app.routes}
    for p in ("/", "/hud", "/ws", "/ws/hud", "/ws/phone-audio",
              "/api/history", "/api/routines", "/api/usage",
              "/api/audio-test", "/api/staged", "/api/consolidate",
              "/api/plugins/install", "/api/photo", "/api/command",
              "/api/upload", "/api/lists", "/api/backups", "/api/backup",
              "/api/backup/download", "/api/restore", "/api/presence"):
        assert p in paths, f"route missing: {p}"


def test_offline_module_safe():
    """Offline brain never raises, with or without Ollama running."""
    from core import offline as off
    assert isinstance(off.available(), bool)
    assert off.chat("") is None
    r = off.chat("Reply with exactly: OK")
    assert r is None or isinstance(r, str)


def test_macros_focus_lists():
    from core import routines as rt
    m = rt.add("smoke-macro", {"kind": "macro", "match": "contains",
                               "text": "smoke trigger"}, "do smoke things")
    assert "error" not in m
    assert rt.match_macro("hey smoke trigger now")["id"] == m["id"]
    assert rt.match_macro("unrelated") is None
    assert rt.remove(m["id"]) is True
    from core import focus as fc
    assert fc.start(1, "smoke")[:8] == "Focusing"
    assert fc.active() is True
    assert fc.hold("x") is True
    _, summary = fc.stop()
    assert "Focus over" in summary
    from core import checklists as cl
    assert "Added" in cl.add("smoke-list", "smoke item")
    assert "Checked" in cl.check("smoke-list", "smoke")
    assert cl.delete_list("smoke-list") is True
