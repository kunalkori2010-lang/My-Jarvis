"""
Gmail plugin — read and send mail by voice.

Modes:
  unread — newest unread subjects (default, no arguments needed)
  search — query Gmail ("from:boss", "invoices", ...)
  send   — irreversible, so it parks behind the on-screen CONFIRM gate like
           shutdown/restart do; nothing sends until the user presses CONFIRM.
"""

from plugins._google_core import SCOPES_GMAIL, get_service

try:
    from core import confirm as _confirm
except Exception:
    _confirm = None


def _short(s, n=80):
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "…"


def _headers(payload):
    out = {}
    try:
        for h in payload.get("headers", []):
            out[h.get("name", "").lower()] = h.get("value", "")
    except Exception:
        pass
    return out


def _list_messages(svc, query, limit=5):
    msgs = svc.users().messages().list(
        userId="me", q=query or "", maxResults=max(1, min(int(limit or 5), 10))
    ).execute().get("messages", [])
    lines = []
    for m in msgs:
        try:
            full = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["Subject", "From", "Date"]).execute()
            h = _headers(full.get("payload", {}))
            lines.append(
                f"• {_short(h.get('subject', '(no subject)'), 60)} "
                f"— {_short(h.get('from', '?'), 40)}")
        except Exception:
            continue
    return lines


def _do_send(svc, to, subject, body):
    import base64
    from email.mime.text import MIMEText
    msg = MIMEText(body or "")
    msg["To"] = to
    msg["Subject"] = subject or "(no subject)"
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    svc.users().messages().send(
        userId="me", body={"raw": raw}).execute()
    return f"Email sent to {to}."


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    mode = str(params.get("mode", "unread")).strip().lower()

    svc, err = get_service("gmail", "v1", SCOPES_GMAIL, "token_gmail.json")
    if svc is None:
        return err

    try:
        if mode in ("unread", "inbox", ""):
            lines = _list_messages(svc, "is:unread", params.get("limit", 5))
            if not lines:
                return "Your inbox is clear — nothing unread."
            return "Unread mail:\n" + "\n".join(lines)

        if mode == "search":
            query = str(params.get("query", "")).strip()
            if not query:
                return "What should I search your mail for?"
            lines = _list_messages(svc, query, params.get("limit", 5))
            if not lines:
                return f"Nothing found for '{query}'."
            return f"Mail matching '{query}':\n" + "\n".join(lines)

        if mode == "send":
            to = str(params.get("to", "")).strip()
            subject = str(params.get("subject", "")).strip()
            body = str(params.get("body", "")).strip()
            if not to or not body:
                return "I need a recipient and a message body to send mail."
            if _confirm is None or _confirm.pending_title():
                return ("There is already a confirmation waiting on screen. "
                        "Ask the user to answer that one first."
                        if _confirm else "Confirmation is unavailable right now.")
            return _confirm.request(
                key="gmail-send", title="Send email",
                detail=f"To: {to}\nSubject: {subject or '(no subject)'}",
                run=lambda: _do_send(svc, to, subject, body),
            )

        return f"Unknown Gmail mode '{mode}'. Use unread, search, or send."
    except Exception as e:
        return f"Gmail failed: {e}"


PLUGIN = {
    "name": "gmail",
    "description": (
        "Read and send Gmail. Use when the user asks about email, inbox, "
        "unread mail, or sending an email. Modes: unread, search, send. "
        "Do NOT use web_search for the user's own mailbox."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {"type": "STRING",
                     "description": "unread | search | send (default unread)"},
            "query": {"type": "STRING",
                      "description": "Gmail search query for mode=search"},
            "limit": {"type": "STRING",
                      "description": "How many messages to list (default 5)"},
            "to": {"type": "STRING",
                   "description": "Recipient address for mode=send"},
            "subject": {"type": "STRING",
                        "description": "Subject line for mode=send"},
            "body": {"type": "STRING",
                     "description": "Message body for mode=send"},
        },
        "required": [],
    },
}
