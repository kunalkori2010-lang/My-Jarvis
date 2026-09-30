"""
Google Calendar plugin — today's agenda, upcoming events, and quick adds.

Modes:
  today    — what is on the calendar today (default)
  upcoming — next N days (days=7)
  add      — create an event (summary + start; end optional, defaults +1h).
             Accepts "tomorrow 18:00", "2026-10-02 09:30", or a bare
             "18:00" meaning today.
"""

from datetime import datetime, timedelta

from plugins._google_core import SCOPES_CALENDAR, get_service


def _parse_when(raw: str) -> datetime | None:
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    now = datetime.now()
    try:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%d-%m-%Y %H:%M",
                    "%d/%m/%Y %H:%M", "%H:%M"):
            try:
                dt = datetime.strptime(raw, fmt)
                if fmt == "%H:%M":
                    dt = now.replace(hour=dt.hour, minute=dt.minute,
                                     second=0, microsecond=0)
                    if dt < now:
                        dt += timedelta(days=1)
                elif len(raw) == 10 and fmt.startswith("%Y"):
                    dt = dt.replace(hour=9, minute=0)
                return dt
            except ValueError:
                continue
        if raw.startswith("tomorrow"):
            day = (now + timedelta(days=1)).date()
            rest = raw[len("tomorrow"):].strip()
            if rest:
                t = datetime.strptime(rest, "%H:%M").time()
                return datetime.combine(day, t)
            return datetime.combine(day, datetime.min.time()).replace(hour=9)
        if raw == "today":
            return now
    except Exception:
        return None
    return None


def _fmt(ev: dict) -> str:
    try:
        summary = ev.get("summary", "(no title)")
        start = ev.get("start", {}).get("dateTime",
                                        ev.get("start", {}).get("date", "?"))
        try:
            dt = datetime.fromisoformat(start)
            when = dt.strftime("%a %H:%M")
        except Exception:
            when = str(start)
        return f"• {when} — {summary}"
    except Exception:
        return "• (unreadable event)"


def run(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    mode = str(params.get("mode", "today")).strip().lower()

    svc, err = get_service("calendar", "v3", SCOPES_CALENDAR,
                           "token_calendar.json")
    if svc is None:
        return err

    try:
        if mode in ("today", "agenda", ""):
            start = datetime.now().replace(hour=0, minute=0,
                                           second=0, microsecond=0)
            end = start + timedelta(days=1)
            items = svc.events().list(
                calendarId="primary", timeMin=start.isoformat() + "Z",
                timeMax=end.isoformat() + "Z", singleEvents=True,
                orderBy="startTime", maxResults=20).execute().get("items", [])
            if not items:
                return "Nothing on your calendar today."
            return "Today:\n" + "\n".join(_fmt(e) for e in items)

        if mode in ("upcoming", "week", "next"):
            try:
                days = max(1, min(int(params.get("days", 7)), 30))
            except Exception:
                days = 7
            now = datetime.now()
            items = svc.events().list(
                calendarId="primary", timeMin=now.isoformat() + "Z",
                timeMax=(now + timedelta(days=days)).isoformat() + "Z",
                singleEvents=True, orderBy="startTime",
                maxResults=30).execute().get("items", [])
            if not items:
                return f"Nothing in the next {days} days."
            return f"Next {days} days:\n" + "\n".join(_fmt(e) for e in items)

        if mode in ("add", "create"):
            summary = str(params.get("summary", "")).strip()
            start = _parse_when(str(params.get("start", "")))
            if not summary or start is None:
                return ("To add an event I need a title and a start time, "
                        "e.g. summary='Gym', start='tomorrow 11:00'.")
            end = _parse_when(str(params.get("end", ""))) or (
                start + timedelta(hours=1))
            ev = svc.events().insert(calendarId="primary", body={
                "summary": summary,
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
            }).execute()
            return (f"Added '{summary}' on "
                    f"{start.strftime('%a %d %b at %H:%M')}.")

        return f"Unknown Calendar mode '{mode}'. Use today, upcoming, or add."
    except Exception as e:
        return f"Calendar failed: {e}"


PLUGIN = {
    "name": "calendar",
    "description": (
        "Google Calendar: today's agenda, upcoming events, and adding events. "
        "Use when the user asks about their schedule, meetings, or adding a "
        "reminder-like event. Modes: today, upcoming, add."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "mode": {"type": "STRING",
                     "description": "today | upcoming | add (default today)"},
            "days": {"type": "STRING",
                     "description": "Days ahead for mode=upcoming (default 7)"},
            "summary": {"type": "STRING",
                        "description": "Event title for mode=add"},
            "start": {"type": "STRING",
                      "description": "Start for mode=add, e.g. 'tomorrow 18:00'"},
            "end": {"type": "STRING",
                    "description": "Optional end for mode=add (default +1h)"},
        },
        "required": [],
    },
}
