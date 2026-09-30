"""
Shared Google OAuth for the Gmail / Calendar plugins (NOT a plugin itself —
the leading underscore keeps the loader from discovering it).

Token files are named token_<app>.json so `.gitignore`'s `**/token*.json`
rule always covers them. Never rename them to something cuter without
checking the ignore rules first.
"""

from __future__ import annotations

import json
from pathlib import Path

SCOPES_GMAIL = ["https://www.googleapis.com/auth/gmail.modify"]
SCOPES_CALENDAR = ["https://www.googleapis.com/auth/calendar"]

_SETUP = (
    "Google is not connected yet. On the computer running JARVIS: "
    "1) create an OAuth Desktop credential at console.cloud.google.com, "
    "2) save it as config/client_secret.json, "
    "3) run once and complete the browser sign-in. "
    "Then ask me again."
)


def _base_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _find_client_secret() -> Path | None:
    cfg = _base_dir() / "config"
    for name in ("client_secret.json", "client_secret_desktop.json",
                 "credentials.json"):
        p = cfg / name
        if p.exists():
            return p
    return None


def get_service(api: str, version: str, scopes: list, token_name: str):
    """(service, None) on success, (None, setup-or-error-text) otherwise.

    Never raises — plugins must return strings, not tracebacks.
    """
    try:
        from googleapiclient.discovery import build
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        return None, ("Google libraries are missing. Run: "
                      "pip install google-api-python-client google-auth-oauthlib")

    token_path = _base_dir() / "config" / token_name
    creds = None
    try:
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(token_path), scopes)
    except Exception:
        creds = None
    try:
        if creds and creds.valid:
            pass
        elif creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            secret = _find_client_secret()
            if secret is None:
                return None, _SETUP
            flow = InstalledAppFlow.from_client_secrets_file(
                str(secret), scopes)
            creds = flow.run_local_server(port=0)
            token_path.write_text(creds.to_json(), encoding="utf-8")
        return build(api, version, credentials=creds), None
    except Exception as e:
        return None, f"Google sign-in failed ({e}). {_SETUP}"
