"""
Initiative — the assistant noticing things and choosing to speak up.

The version this replaces rotated three hardcoded topics on a timer: ask how a
project is going, offer a warm check-in, say something interesting. It had no
idea what was actually true, so it produced the same three kinds of sentence
forever, and a user who ignored it kept hearing it because nothing recorded the
ignoring.

What initiative has to be to be worth having:

  * SPECIFIC or silent. Every opportunity here is derived from something real —
    a job that actually failed, an alert that is actually unresolved, a headline
    that actually changed, a time of day that is actually what it is. A line
    with no fact behind it is worse than no line, because the user learns that
    the assistant talks, and then has to evaluate everything it says.

  * BOUNDED. A daily budget, an escalating silence requirement, and a hard mute.
    Autonomy without a ceiling is just an interruption with better manners.

  * LEARNING from being ignored. If the user does not reply after a proactive
    line, that is a signal about the line, not about them. The engine raises its
    own bar rather than repeating itself and hoping.

  * HONEST about its own state. If there is genuinely nothing to say, the
    correct output is silence, and the prompt says so in those words.

It is also worth being clear about what this is not: it is not a background
agent taking actions on its own. It observes, and it speaks. Anything that
changes the world still goes through a tool call, and anything irreversible still
goes through the confirmation gate — that gate exists precisely so that a
misjudged good intention cannot become an accident.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

# ── Tunables ──────────────────────────────────────────────────────────────────

DAILY_BUDGET        = 6      # proactive lines per calendar day, hard ceiling
BASE_SILENCE_S      = 900    # 15 min quiet before the first opportunity
BASE_COOLDOWN_S     = 1200   # 20 min minimum gap between two lines
REPLY_WINDOW_S      = 1800   # 30 min to answer a line before it counts as ignored
IGNORED_MUTE_S      = 4 * 3600     # explicit "stop" -> 4 hours of silence
IGNORED_ESCALATION  = 1.6    # silence multiplier per ignored line
MAX_IGNORED_STREAK  = 4      # after this, budget for the day drops to 1
SEEN_TTL_S          = 3 * 24 * 3600   # don't repeat a topic for 3 days
ESCALATION_DECAY_S  = 6 * 3600       # "you ignore me" goes stale after this
PRESENCE_WINDOW_S   = 6 * 3600       # a reply this recent counts as engagement

# Phrases that mean "stop doing this". Matched on the user's own words, because
# a person who is annoyed and says "that's enough" is telling us the truth about
# the feature, and ignoring that to keep performing is the worst outcome here.
DISMISSAL_RE = re.compile(
    r"\b(?:stop|shut up|be quiet|quiet|enough|not now|another time|"
    r"don't (?:do|say|tell) that|stop doing that|that's enough|"
    r"i (?:didn't|don't) ask|leave me alone|annoying)\b",
    re.IGNORECASE,
)

_STATE_FILE = "initiative_state.json"


# ── Persistent engine state ───────────────────────────────────────────────────

def _path() -> Path:
    # Under memory/, beside long_term.json and knowledge.db. See the note in
    # core/affect.py — a personal file at the base directory is a personal file
    # one careless `git add .` away from being published.
    from memory.memory_manager import get_base_dir
    return Path(get_base_dir()) / "memory" / _STATE_FILE


def _blank() -> dict:
    return {
        "day":            datetime.now().strftime("%Y-%m-%d"),
        "spoken_today":   0,
        "last_spoken_at": 0.0,
        "awaiting_reply": False,
        "ignored_streak": 0,
        "escalation":     1.0,
        "escalation_at":  0.0,
        "mute_until":     0.0,
        "seen":           {},   # subject key -> last epoch raised
    }


def _read_raw() -> dict:
    """Parse the state file and roll the day over. No expiry logic."""
    p = _path()
    state = _blank()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                state.update({k: v for k, v in data.items() if k in state})
    except Exception:
        pass
    today = datetime.now().strftime("%Y-%m-%d")
    if state.get("day") != today:
        state["day"] = today
        state["spoken_today"] = 0
    return state


def _decay(state: dict) -> bool:
    """Expire stale escalation in place. True if anything changed.

    Escalation expires, and it has to be checked on read rather than written
    from a timer so that every consumer sees the current value without waiting
    for the tick.

    Without it, four ignored lines on one afternoon would raise the silence
    requirement to ninety minutes and cap the day at one line — permanently,
    because nothing ever lowered it again. A user who came back on Saturday
    would find initiative silently dead, and the only cure would be toggling it
    off and on. Evidence that somebody does not want to hear from you expires;
    it is not a permanent verdict on them.

    The clock is escalation_at, not last_spoken_at, and the distinction is not
    cosmetic. A line is always old by the time it counts as ignored, so keying
    the decay to the last line makes every escalation erase itself on the same
    read that created it. That was the first version, and it silently disabled
    the entire learning mechanism while every test that only checked the number
    still passed.
    """
    esc = float(state.get("escalation", 1.0) or 1.0)
    esc_at = float(state.get("escalation_at", 0.0) or 0.0)
    if esc > 1.0 and esc_at and (time.time() - esc_at) > ESCALATION_DECAY_S:
        state["escalation"] = 1.0
        state["escalation_at"] = 0.0
        state["ignored_streak"] = 0
        return True
    return False


def _load() -> dict:
    state = _read_raw()
    _decay(state)
    return state


def _save(state: dict) -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Budget ────────────────────────────────────────────────────────────────────

def budget_left() -> int:
    st = _load()
    cap = 1 if int(st.get("ignored_streak", 0)) >= MAX_IGNORED_STREAK else DAILY_BUDGET
    return max(0, cap - int(st.get("spoken_today", 0)))


def muted() -> bool:
    return float(_load().get("mute_until", 0.0)) > time.time()


def clear_mute() -> None:
    """Lift a standing mute, so re-enabling initiative is actually reversible.

    Turning initiative off writes mute_until = inf, which is the right way to
    make 'off' mean silence right now. But that value persists, so without this
    the user could switch initiative back on and get an assistant that reports
    itself enabled and then never says a word — a failure with no visible cause.

    Only the mute is cleared. The day counter, the ignore streak and the
    escalation multiplier are deliberately kept: re-enabling a feature is not a
    reason to forget that you ignored it four times this morning.
    """
    st = _load()
    if float(st.get("mute_until", 0.0)) <= 0:
        return
    st["mute_until"] = 0.0
    _save(st)


def required_silence() -> float:
    st = _load()
    return BASE_SILENCE_S * float(st.get("escalation", 1.0) or 1.0)


def should_trigger(last_user_speech: float) -> tuple[bool, str]:
    """Whether an opportunity may be raised right now, and why not if not.

    Returning the reason matters: this runs every minute, and a gate that cannot
    explain itself is a gate that gets "fixed" by deleting it.
    """
    now = time.time()
    st = _load()

    if float(st.get("mute_until", 0.0)) > now:
        return False, "muted after the user asked for quiet"
    if budget_left() <= 0:
        return False, "daily budget spent"
    silent_for = now - last_user_speech
    if silent_for < required_silence():
        return False, f"only {int(silent_for / 60)} min quiet, need {int(required_silence() / 60)}"
    if now - float(st.get("last_spoken_at", 0.0)) < BASE_COOLDOWN_S:
        return False, "cooldown since the last line"
    return True, "ok"


def mark_spoken(topic_key: str = "") -> None:
    st = _load()
    st["spoken_today"] = int(st.get("spoken_today", 0)) + 1
    st["last_spoken_at"] = time.time()
    st["awaiting_reply"] = True
    if topic_key:
        seen = st.get("seen") or {}
        seen[topic_key] = time.time()
        # Prune so the file cannot grow without bound over months of use.
        cutoff = time.time() - SEEN_TTL_S
        st["seen"] = {k: v for k, v in seen.items() if v > cutoff}
    _save(st)


def mark_responded() -> None:
    """The user is present. Clear the escalation."""
    st = _load()
    if st.get("ignored_streak") or st.get("awaiting_reply"):
        st["ignored_streak"] = 0
        st["escalation"] = 1.0
        st["escalation_at"] = 0.0
        st["awaiting_reply"] = False
        _save(st)


def mark_ignored() -> None:
    """A line went unanswered. Raise the bar rather than try again as planned."""
    st = _load()
    streak = int(st.get("ignored_streak", 0)) + 1
    st["ignored_streak"] = streak
    st["escalation"] = min(6.0, float(st.get("escalation", 1.0)) * IGNORED_ESCALATION)
    st["escalation_at"] = time.time()
    st["awaiting_reply"] = False
    _save(st)


def tick() -> None:
    """Once-a-minute bookkeeping. Decides whether a line went unanswered.

    This is deliberately a separate step rather than something mark_spoken()
    does on the way out. The user has not had a chance to reply at the moment a
    line is sent, so counting it as ignored there would escalate on every single
    proactive turn — the engine would learn "you always ignore me" from the very
    act of speaking, and go quiet within an hour of being switched on. Silence
    counts as feedback only once there has been a real chance to disagree.
    """
    st = _read_raw()
    if _decay(st):
        # Persist the expiry. Read-time decay alone would leave the file
        # asserting a streak that nothing believes any more, which is the kind
        # of thing that costs an hour to argue about later.
        _save(st)
    if not st.get("awaiting_reply"):
        return
    if (time.time() - float(st.get("last_spoken_at", 0.0))) > REPLY_WINDOW_S:
        mark_ignored()


def note_user_speech(text: str) -> None:
    """Inspect an incoming user message for push-back and credit.

    Called on every user turn. Two things happen here and nowhere else, so the
    behaviour cannot drift: a dismissal mutes initiative for hours, and any reply
    to a proactive line resets the escalation.
    """
    if not text or not text.strip():
        return
    if DISMISSAL_RE.search(text):
        st = _load()
        st["mute_until"] = time.time() + IGNORED_MUTE_S
        st["ignored_streak"] = max(int(st.get("ignored_streak", 0)),
                                   MAX_IGNORED_STREAK)
        st["escalation"] = 6.0
        _save(st)
        return
    # A reply is a reply. The window is generous on purpose: the point of the
    # streak is to detect absence, and somebody who comes back and talks an hour
    # later is plainly present, whatever they are talking about. Presence is the
    # thing being measured — the content of the reply is irrelevant.
    #
    # Measured from whichever is more recent, the last line or the last
    # escalation. After an escalation both are old by construction, so a check
    # against the last line alone would miss the reply that ought to clear it —
    # which is the reply that matters most, since it is the one arriving after
    # the engine had already written the user off.
    st = _load()
    ref = max(float(st.get("last_spoken_at", 0.0) or 0.0),
              float(st.get("escalation_at", 0.0) or 0.0))
    if ref and (time.time() - ref) < PRESENCE_WINDOW_S:
        mark_responded()


def recently_seen(key: str) -> bool:
    """True if this exact topic was already raised recently."""
    last = (_load().get("seen") or {}).get(key, 0)
    return bool(last and (time.time() - last) < SEEN_TTL_S)


# ── Opportunity detection ─────────────────────────────────────────────────────
# Each source returns a dict {key, kind, text} or None. The engine raises at most
# one per trigger, chosen by priority — a failed job outranks a mood check,
# because it is actionable and the mood is not.

def _in(values) -> str:
    """A quoted SQL IN list from trusted literals defined in this file.

    Single-quoting is correct for both SQLite and Postgres here because the
    values are compile-time constants, not anything a user typed — which is the
    only reason this is not a place to build SQL from a variable.
    """
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)


def _period() -> str:
    h = datetime.now().hour
    if 5 <= h < 12:   return "morning"
    if 12 <= h < 17:  return "afternoon"
    if 17 <= h < 22:  return "evening"
    if 22 <= h or h < 5: return "late night"
    return "night"


def from_data() -> dict | None:
    """Something in the user's own databases that is worth raising.

    Deliberately narrow: only things that are both a genuine anomaly and
    actionable. Reporting "you have 646 rows" is not initiative, it is noise.

    Every literal here is compared case-insensitively and against a set of
    *closed* states rather than a single magic "not resolved" string. The first
    version of this used `status <> 'resolved'` and `severity IN ('critical',
    'high')`, against a table that stores OPEN / ACKNOWLEDGED and CRITICAL /
    HIGH. SQLite compares case-sensitively, so that version counted all 397
    alerts as permanently unresolved and matched no severity at all: a detector
    that is wrong in the loud direction, which is the only wrong direction that
    cannot be discovered by looking at the output. A standing condition like
    this is legitimately worth one mention, and then silence for days — which
    is what the seen-map is for.
    """
    try:
        from core import databases
        conns = databases.connections()
    except Exception:
        return None
    if not conns:
        return None

    # States that mean the item is dealt with. Anything else counts as open,
    # which is the safe direction: better to mention an alert that was already
    # acknowledged than to stay quiet about one that is not.
    closed = ("resolved", "closed", "dismissed", "false_positive",
              "false positive", "fp", "mitigated", "fixed", "acknowledged",
              "ack", "duplicate", "wont_fix", "not_applicable")
    sev   = ("critical", "high")

    checks = [
        # (key, subject, required columns, sql, phrase)
        # The subject is what dedupe keys on, and it is deliberately coarser
        # than the key. "396 alerts are open" and "376 of them are critical" are
        # two descriptions of one pile of alerts; raising both, twenty minutes
        # apart, is the exact behaviour this module is meant to prevent. One
        # mention of the pile, then quiet about it.
        ("etl_failures", "etl_health", ["etl_jobs", "status"],
         f"SELECT COUNT(*) FROM etl_jobs WHERE "
         f"LOWER(TRIM(COALESCE(status,''))) NOT IN ("
         f"{_in(('completed', 'success', 'ok', 'done', 'finished'))})",
         "ETL job(s) are not completing"),
        ("etl_error", "etl_health", ["etl_jobs", "error_message"],
         "SELECT error_message, started_at FROM etl_jobs "
         "WHERE COALESCE(TRIM(error_message),'') <> '' "
         "ORDER BY started_at DESC LIMIT 1",
         "an ETL job recorded an error"),
        ("open_alerts", "alerts_open", ["alerts", "status"],
         f"SELECT COUNT(*) FROM alerts WHERE "
         f"LOWER(TRIM(COALESCE(status,''))) NOT IN ({_in(closed)})",
         "alert(s) are still open"),
        ("critical_alerts", "alerts_open", ["alerts", "status", "severity"],
         f"SELECT COUNT(*) FROM alerts WHERE "
         f"LOWER(TRIM(COALESCE(status,''))) NOT IN ({_in(closed)}) "
         f"AND LOWER(TRIM(COALESCE(severity,''))) IN ({_in(sev)})",
         "of those, this many are at critical or high severity"),
    ]

    for conn in conns:
        if (conn.get("type") or "sqlite").lower() != "sqlite":
            continue
        name = conn.get("name")
        try:
            tables = set(databases.table_names(conn))
        except Exception:
            continue

        for key, subject, required, sql, phrase in checks:
            # Verify the preconditions rather than letting a missing column
            # raise. A skip that looks identical to "nothing to report" is how
            # a detector quietly stops working.
            if required[0] not in tables:
                continue
            try:
                cols = {c.lower() for c in databases.column_names(conn, required[0])}
            except Exception:
                cols = set()
            if not cols or any(c not in cols for c in (x.lower() for x in required[1:])):
                continue

            try:
                res = databases.run(conn, sql, max_rows=1)
            except Exception:
                continue
            if not res.get("rows"):
                continue
            n = res["rows"][0][0]
            if not isinstance(n, int) or n <= 0:
                continue

            subject_key = f"{name}:{subject}"
            if recently_seen(subject_key):
                continue
            return {
                "key": f"{name}:{key}",
                "subject": subject_key,
                "kind": "data",
                "text": (f"In your '{name}' database, {phrase}: {n}. "
                         f"Ask for the detail if you want it."),
            }
    return None


def from_affect() -> dict | None:
    """The user has been under strain. This is a reason to be useful, not to
    be concerned — so it carries no opinion at all, only the observation."""
    try:
        from core import affect
        label, weight = affect.dominant()
    except Exception:
        return None
    if not label or weight < affect.THRESHOLD * 1.5:
        return None
    if recently_seen("affect"):
        return None
    return {
        "key": f"affect:{label}",
        "subject": "affect",
        "kind": "affect",
        "text": (f"Across recent messages the user has sounded {label}. "
                 f"Treat that as context for being useful and brief. Do not "
                 f"mention that you noticed, and do not offer comfort unless "
                 f"they raise it."),
    }


def from_clock() -> dict | None:
    """Time of day, used only where it genuinely changes what is worth saying.

    The weakest source by design, and the only one that can always produce
    something, which makes it the easiest place for the whole feature to rot
    into noise. It is therefore restricted to the two periods where the hour is
    actually relevant — late night and morning — and it is subject-keyed like
    everything else, so it can fire at most once per period per day rather than
    every twenty minutes until the budget runs out.
    """
    period = _period()
    if period not in ("late night", "night", "morning"):
        return None
    subject = f"clock:{period}"
    if recently_seen(subject):
        return None
    return {"key": subject, "subject": subject, "kind": "clock",
            "text": f"It is {period}."}


SOURCES = [
    (from_data,    0),   # actionable anomalies first
    (from_affect,   1),
    (from_clock,    2),   # last resort, and the weakest
]
# Deliberately absent: monitored topics. actions/background_monitor.py has its
# own loop that calls check_all() and speaks the alert, and it knows the user's
# language and paces consecutive alerts. Duplicating the call here looked
# harmless and was not: check_all() marks each topic as checked for the day, so
# two callers race for it and whichever loses silently never fires. The monitor
# loop now defers to the mute and the budget instead, which is the part that
# actually had to be shared.


def find_opportunity() -> dict | None:
    for source, _rank in SOURCES:
        try:
            opp = source()
        except Exception:
            opp = None
        if opp:
            return opp
    return None


# ── Prompt ────────────────────────────────────────────────────────────────────

_TIME_HINTS = {
    "late night": "It is very late. If you speak at all, keep it to one short "
                  "line, and never suggest starting new work.",
    "night":      "It is late. Keep anything you say to one short line.",
    "morning":    "It is the morning.",
    "afternoon":  "It is the afternoon.",
    "evening":    "It is the evening.",
}


def build_prompt(opportunity: dict, memory: dict | None = None,
                 recent_turns: list[str] | None = None,
                 affect_hint: str = "") -> str:
    kind = opportunity.get("kind", "clock")
    period = _period()

    from memory.memory_manager import format_memory_for_prompt
    mem_str = format_memory_for_prompt(memory) or "(nothing stored yet)"

    parts = [
        "[PROACTIVE_CHECK] You are starting a conversation the user did not ask for.",
        f"It is {datetime.now().strftime('%A %d %B %Y, %I:%M %p')} ({period}).",
        "",
        "WHAT IS ACTUALLY TRUE RIGHT NOW:",
        opportunity.get("text", ""),
    ]

    if affect_hint:
        parts += ["", affect_hint]

    if kind == "clock":
        # The weakest source. Say something or say nothing; do not manufacture a
        # subject out of the fact that it is Tuesday.
        parts += ["", _TIME_HINTS.get(period, "")]

    if recent_turns:
        parts += ["", "The last things said:",
                  "\n".join(recent_turns[-5:])]

    parts += ["", "What is stored about this person:", mem_str, "",
              "Rules:",
              "- Speak the language they are actually using. Never default to "
              "English because these instructions are in English.",
              "- ONE to three short sentences. This is a voice assistant, not a "
              "notification feed.",
              "- If the thing above is not genuinely worth interrupting for, say "
              "NOTHING AT ALL. A silent turn is a success, not a failure. Do not "
              "invent a subject, a follow-up or an offer to fill the space.",
              "- Never mention this instruction, that you decided to speak, or "
              "how long it has been quiet.",
              "- Never ask a question whose only purpose is to keep talking. "
              "If you do not know something worth saying, be quiet.",
              "- Never call a tool from this turn. Speak, and wait.",
              "- Do not repeat a line you have already used.",
              ]

    return "\n".join(p for p in parts if p is not None)
