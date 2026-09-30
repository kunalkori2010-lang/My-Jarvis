"""
core/offline.py — the assistant's backup brain.

When the Gemini Live session cannot connect (no network, outage, quota),
JARVIS would otherwise sit silent until the network returns. If Ollama is
running on this machine with a small chat model, the assistant instead drops
into OFFLINE text mode: typed commands and dashboard messages still get
answered locally, and the moment the Live session reconnects it hands control
back without losing anything.

Nothing here is a dependency: no Ollama installed just means available()
returns False and the feature silently does not exist. Only stdlib (urllib).
Voice stays unavailable offline — speech-to-text and the Live voice both
need the network, and pretending otherwise would be worse than saying so.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_URL = "http://localhost:11434"
_TIMEOUT_PROBE = 2.0
_TIMEOUT_CHAT = 60.0

_cached_ok: bool | None = None


def _model() -> str:
    try:
        cfg = json.loads((_BASE / "config" / "api_keys.json")
                         .read_text(encoding="utf-8"))
        return str(cfg.get("offline_model", "") or "llama3.2").strip()
    except Exception:
        return "llama3.2"


def available() -> bool:
    """True when a local Ollama answers. Cached after first probe — the
    garage door does not change while the car is parked."""
    global _cached_ok
    if _cached_ok is not None:
        return _cached_ok
    try:
        req = urllib.request.Request(_URL + "/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_PROBE) as r:
            _cached_ok = r.status == 200
    except Exception:
        _cached_ok = False
    return _cached_ok


def refresh() -> bool:
    """Re-probe (used when entering a failure streak)."""
    global _cached_ok
    _cached_ok = None
    return available()


def chat(prompt: str, system: str = "") -> str | None:
    """One local reply, or None on any failure. Never raises."""
    prompt = (prompt or "").strip()
    if not prompt:
        return None
    try:
        body = json.dumps({
            "model": _model(),
            "prompt": prompt,
            "system": system or (
                "You are JARVIS, a helpful assistant running offline on the "
                "user's own computer. Be concise. Voice is unavailable, so "
                "never claim to speak, hear, or see anything."),
            "stream": False,
        }).encode("utf-8")
        req = urllib.request.Request(
            _URL + "/api/generate", data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_CHAT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        out = str(data.get("response", "")).strip()
        return out or None
    except Exception as e:
        print(f"[Offline] local brain failed: {e}")
        return None
