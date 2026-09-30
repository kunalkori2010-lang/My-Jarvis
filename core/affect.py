"""
Attunement — noticing how the user sounds, so replies can land on the person
and not just the question.

What this is, precisely: a small lexicon that reads a user's message for signs
of distress, frustration, fatigue, confusion, urgency, warmth and resignation,
accumulates those readings into a rolling state that decays over hours, and hands
the model one short honest sentence about what it has noticed.

What this is NOT, and the distinction is the whole design:

  * It is not simulated emotion. Nothing here has any inner state, and the model
    is never told it does. A program that claims to be sad because a string
    matched a regex is worse than one that simply pays attention, because it
    teaches the user that the warmth is real and therefore that the competence is
    theatre too.

  * It is not diagnosis. Nothing here classifies a mental-health condition, and
    there is deliberately no lexicon that could. "Stressed" is a thing a person
    says about their Tuesday; it is not a symptom, and treating it as one turns a
    voice assistant into something unpleasant to talk to.

  * It is not sentiment scoring for its own sake. A valence number nobody reads is
    a vanity metric. The only output that matters is the guidance string handed to
    the model, and it is written to make the model MORE accurate, not chattier.

The hard part is restraint. A naive version of this says "the user seems stressed"
after the word "stress" appears once, and then the assistant starts performing
concern for a day — which is both wrong and unbearable. So every reading has to
clear three bars before it is allowed to affect anything: corroboration across
messages, intensity, and the fact that it has not simply decayed away. Single weak
signals are recorded and then ignored on purpose.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# ── Signal table ──────────────────────────────────────────────────────────────
# Each entry: stem -> (label, valence, weight, needs_corroboration)
#
# valence is -1 (unpleasant) .. +1 (pleasant) and is only used to build a rolling
# mood number, never to decide tone on its own. weight is how much a single
# occurrence counts. needs_corroboration marks a word that on its own proves
# nothing — "tired" in "the build is tired" or "fine" in "fine, whatever" are
# ordinary usages, and treating them as emotional evidence is how this feature
# becomes a nuisance.

SIGNALS: dict[str, tuple[str, float, float, bool]] = {
    # distress — the ones that matter most, and the easiest to over-read
    "stress":  ("distress",     -0.7, 1.0, True),
    "anxious": ("distress",     -0.7, 1.0, False),
    "anxiety": ("distress",     -0.7, 1.0, False),
    "worried": ("distress",     -0.5, 0.8, True),
    "worry":   ("distress",     -0.5, 0.8, True),
    "nervous": ("distress",     -0.5, 0.8, False),
    "panic":   ("distress",     -0.9, 1.3, False),
    "overwhelm": ("distress",   -0.7, 1.0, False),
    "burnout": ("distress",     -0.8, 1.0, False),
    "burnt":   ("distress",     -0.8, 0.9, False),
    "hopeless": ("distress",    -0.9, 1.2, False),
    "depress": ("distress",     -0.9, 1.2, False),
    "alone":   ("distress",     -0.5, 0.7, True),
    "lonely":  ("distress",     -0.6, 0.9, False),

    # frustration — almost always about a task, so it wants a fix not a hug
    "frustrat": ("frustration", -0.6, 0.9, False),
    "annoy":   ("frustration", -0.5, 0.8, True),
    "irritat": ("frustration", -0.5, 0.8, False),
    "angry":   ("frustration", -0.7, 1.0, False),
    "mad":     ("frustration", -0.6, 0.9, True),
    "hate":    ("frustration", -0.7, 0.9, True),
    "stupid":  ("frustration", -0.5, 0.7, True),
    "useless": ("frustration", -0.7, 1.0, False),
    "ridiculous": ("frustration", -0.5, 0.8, False),
    "useless":  ("frustration", -0.7, 1.0, False),
    "again":   ("frustration", -0.3, 0.5, True),
    "still":   ("frustration", -0.3, 0.4, True),

    # fatigue — time-of-day dependent, so weak on its own
    "tired":   ("fatigue",     -0.4, 0.7, True),
    "exhaust": ("fatigue",     -0.5, 0.9, False),
    "sleep":   ("fatigue",     -0.3, 0.5, True),
    "drained": ("fatigue",     -0.5, 0.9, False),
    "sore":    ("fatigue",     -0.3, 0.5, True),
    "headache": ("fatigue",    -0.4, 0.7, True),
    "sick":    ("fatigue",     -0.5, 0.8, True),

    # confusion — the assistant's own doing, often; needs a plain answer first
    "confus":  ("confusion",   -0.3, 0.7, False),
    "unclear": ("confusion",   -0.2, 0.5, True),
    "understand": ("confusion", -0.2, 0.3, True),   # "I don't understand"
    "meaning": ("confusion",   -0.2, 0.3, True),
    "huh":     ("confusion",   -0.2, 0.6, True),
    "lost":    ("confusion",   -0.4, 0.7, True),

    # urgency — not negative, but changes the shape of the reply
    "urgent":  ("urgency",      0.0, 0.9, False),
    "asap":    ("urgency",      0.0, 1.0, False),
    "hurry":   ("urgency",      0.0, 0.8, True),
    "quickly": ("urgency",      0.0, 0.6, True),
    "now":     ("urgency",      0.0, 0.3, True),
    "deadline": ("urgency",     0.0, 0.8, True),

    # resignation — a withdrawal signal, quietly serious
    "whatever": ("resignation", -0.4, 0.8, True),
    "forget":   ("resignation", -0.3, 0.5, True),
    "give up":  ("resignation", -0.7, 1.1, False),
    "gave up":  ("resignation", -0.7, 1.1, False),
    "giving up": ("resignation", -0.7, 1.1, False),
    "pointless": ("resignation", -0.7, 1.0, False),
    "doesnt matter": ("resignation", -0.5, 0.9, False),

    # warmth in — the assistant has done something right
    "thank":   ("gratitude",    0.6, 0.8, False),
    "thanks":  ("gratitude",    0.6, 0.8, False),
    "appreciat": ("gratitude",  0.7, 0.9, False),
    "perfect": ("gratitude",    0.7, 0.9, True),
    "exactly": ("gratitude",    0.5, 0.6, True),
    "worked":  ("gratitude",    0.5, 0.7, True),
    "amazing": ("gratitude",    0.8, 0.9, True),
    "love":    ("joy",          0.8, 0.9, True),
    "brilliant": ("joy",        0.8, 0.9, False),

    # joy / relief
    "happy":   ("joy",          0.8, 0.9, False),
    "excited": ("joy",          0.8, 0.9, False),
    "awesome": ("joy",          0.8, 0.9, False),
    "great":   ("joy",          0.6, 0.5, True),
    "finally": ("relief",       0.6, 0.8, True),
    "yay":     ("joy",          0.8, 1.0, False),
    "passed":  ("relief",       0.6, 0.7, True),
    "works":   ("relief",       0.5, 0.6, True),
}

# label -> valence, built once. Several stems share a label, and the alternative
# (scanning the whole table per hit) is both slower and easier to get wrong than
# a dict lookup.
_LABEL_VALENCE: dict[str, float] = {}
for _sig in SIGNALS.values():
    _LABEL_VALENCE.setdefault(_sig[0], _sig[1])

# Phrases that reverse the sentiment of what follows them. Without these,
# "I'm not stressed" scores as stress, which is precisely backwards.
NEGATIONS = {"not", "no", "never", "dont", "dont", "isnt", "wasnt", "arent",
             "werent", "aint", "hardly", "barely", "without", "cant", "wont"}

INTENSIFIERS = {"very", "really", "so", "extremely", "incredibly", "super",
                "totally", "completely", "absolutely", "utterly", "quite",
                "deeply", "terribly", "awfully", "insanely", "seriously",
                "always", "constantly", "every", "all"}

# A short, flat message carries less evidence than a considered one. Someone
# typing "stressed" alone is terse; someone typing a full sentence about being
# stressed has said more. This scales weight, it does not gate it.
MIN_WORDS_FOR_WEIGHT = 12

_WORD_RE = re.compile(r"[a-z']+")
_AFFECT_FILE = "affect_state.json"

# Labels that describe the speaker's own inner or physical state. These are the
# ones that produce false positives on ordinary sentences: "the build is tired",
# "the old code is ugly", "a confusing error", "a sad song". Nobody says those
# about themselves without a subject, and English makes the subject explicit —
# so requiring a first-person marker within a short window removes the entire
# class of mistake rather than trying to enumerate the nouns involved.
_SELF_LABELS = {"distress", "fatigue", "joy", "gratitude", "relief",
                "resignation", "confusion"}

_SELF_MARKERS = {"i", "im", "ive", "id", "ill", "me", "my", "mine", "myself",
                 "we", "our", "us", "am", "ve", "getting", "felt", "feel",
                 "feeling", "been", "so"}

# Discourse continuations. "yeah, still tired" carries exactly as much
# information about the speaker as "I'm tired" does, but has no first-person
# pronoun in it at all. Without these, the strictest and most common way a person
# reports a continuing state — answering in fragments — would be the one form
# that never registers.
#
# Deliberately only real markers. Generic clause connectors are excluded, because
# "the build is tired AND the code is ugly" is two things being complained about,
# not the speaker describing themselves, and "and" must not be what makes the
# difference.
_CONTINUATION = {"yeah", "yep", "yup", "yes", "nah", "still", "too", "also",
                 "anyway", "well", "ok", "okay", "alright", "honestly",
                 "actually", "definitely", "probably", "maybe", "sure"}

# A very short message has no room for a subject, so "Exhausted." or "ugh" has to
# be allowed to count on its own.
_SELF_EXEMPT_MAX_WORDS = 3

# How fast a reading fades. A bad morning should still be felt at 6pm; something
# said before lunch should not follow someone into the evening.
HALF_LIFE_S = 3.0 * 3600.0


def _load_path() -> Path:
    # Under memory/, beside long_term.json and knowledge.db — not at the base
    # directory, where a frozen build would drop it next to the executable and a
    # source checkout would drop it in the middle of the repository. Both are
    # places a personal file ends up in version control by accident.
    from memory.memory_manager import get_base_dir
    return Path(get_base_dir()) / "memory" / _AFFECT_FILE


def _blank_state() -> dict:
    return {
        "labels": {},        # label -> {"weight": float, "last": epoch, "n": int}
        "valence_sum": 0.0,  # decayed, for a coarse mood number
        "n_observed": 0,
        "mutes_until": 0.0,  # set when the user pushes back on being tuned-in
        "trace": [],         # last few readings, for cross-session continuity
    }


def _load() -> dict:
    p = _load_path()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _blank_state()
                base.update({k: v for k, v in data.items() if k in base})
                return base
    except Exception:
        pass
    return _blank_state()


def _save(state: dict) -> None:
    try:
        p = _load_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        # Losing an affect trace is a cosmetic failure, never worth interrupting
        # a conversation to report.
        pass


# ── Reading one message ───────────────────────────────────────────────────────

def read(text: str) -> dict:
    """Read affect signals out of a single user message.

    Returns {label: strength} for 0..1, plus 'labels' sorted strongest first.
    Strength combines lexicon weight, message length and intensifiers; nothing
    here decides tone, it only supplies evidence for the corroboration step.
    """
    if not text or not text.strip():
        return {}

    lowered = text.lower()
    words = _WORD_RE.findall(lowered)
    if not words:
        return {}

    # Length weight: a considered message is better evidence than a two-word one.
    length_scale = 1.0 if len(words) >= MIN_WORDS_FOR_WEIGHT else 0.75

    # The word pattern keeps apostrophes, so "I'm" arrives as one token and would
    # never match the marker "im". Compare the de-apostrophised form too, or the
    # single most ordinary way of talking about yourself is invisible to this.
    bare = {w.replace("'", "") for w in words}
    has_self = (bool(bare & _SELF_MARKERS)
                or bool(bare & _CONTINUATION)
                or len(words) <= _SELF_EXEMPT_MAX_WORDS)

    hits: dict[str, float] = {}
    for i, w in enumerate(words):
        # Skip anything inside a negation window: "not worried", "no stress".
        if i > 0 and words[i - 1] in NEGATIONS:
            continue
        # Skip when the stem is immediately preceded by a negator anywhere in the
        # last three words — negation in English is not strictly local.
        if any(n in words[max(0, i - 3):i] for n in NEGATIONS):
            continue

        # Multi-word keys first, so "gave up" beats "gave".
        matched = None
        for span in (3, 2):
            if i + span <= len(words):
                phrase = " ".join(words[i:i + span])
                if phrase in SIGNALS:
                    matched = phrase
                    break
        if matched is None:
            # Stem prefix, so "stressed"/"stressing"/"stresses" all hit "stress".
            for stem, sig in SIGNALS.items():
                if " " in stem:
                    continue
                if len(stem) >= 4 and w.startswith(stem):
                    matched = stem
                    break

        if not matched:
            continue
        label, _val, weight, _corr = SIGNALS[matched]

        # A person-state word with no first-person subject in the sentence is
        # almost always about a task, a piece of code or a song. Skip it.
        if label in _SELF_LABELS and not has_self:
            continue

        scale = length_scale
        if any(n in words[max(0, i - 2):i] for n in INTENSIFIERS):
            scale *= 1.35
        # Typing in caps is a real signal of intensity, and of nothing else.
        if w.isupper() and len(w) > 1:
            scale *= 1.2
        # Repeated punctuation is the other one.
        if "!!" in text or "??" in text:
            scale *= 1.15

        hits[label] = hits.get(label, 0.0) + weight * scale

    return {k: v for k, v in hits.items() if v > 0}


# ── Rolling state ─────────────────────────────────────────────────────────────

def observe(text: str) -> dict:
    """Fold a user's message into the persistent rolling state.

    Call this on every user turn. Returns the current state summary.
    """
    state = _load()
    hits = read(text)
    if not hits:
        return state

    now = time.time()
    state = _decay(state, now)

    for label, strength in hits.items():
        # Cap the per-label contribution: one rant should not pin "distress" at
        # maximum for the rest of the day.
        contribution = min(strength, 2.5)
        entry = state["labels"].get(label) or {"weight": 0.0, "last": now, "n": 0}
        entry["weight"] += contribution
        entry["last"] = now
        # Count separate MESSAGES, not repetitions inside one — saying "tired,
        # tired, so tired" is one piece of evidence said loudly, not three.
        entry["n"] = int(entry.get("n", 0)) + 1
        state["labels"][label] = entry
        state["valence_sum"] += _LABEL_VALENCE.get(label, 0.0) * min(contribution, 2.0)

    state["n_observed"] += 1
    state["trace"] = (state.get("trace") or [])[-11:]
    state["trace"].append({
        "t": int(now),
        "labels": sorted(hits, key=hits.get, reverse=True)[:2],
        "weight": round(sum(hits.values()), 2),
    })
    _save(state)
    return state


def _decay(state: dict, now: float) -> dict:
    """Exponentially fade every reading toward zero. Called before each update.

    The occurrence count is deliberately NOT decayed. Corroboration is a fact
    about how often the user has said something, and that stays true; only the
    felt intensity of it fades. Fading the count would mean a person who was
    clearly struggling all morning went back to "unconfirmed" by lunchtime.
    """
    out = {}
    for label, entry in (state.get("labels") or {}).items():
        age = max(0.0, now - float(entry.get("last", now)))
        factor = 0.5 ** (age / HALF_LIFE_S)
        w = float(entry.get("weight", 0.0)) * factor
        if w > 0.05:
            out[label] = {"weight": w, "last": now, "n": int(entry.get("n", 0))}
    state["labels"] = out
    return state


def state() -> dict:
    """Current decayed state, without recording anything."""
    return _decay(_load(), time.time())


def dominant() -> tuple[str | None, float]:
    """The strongest surviving label and its strength, or (None, 0.0)."""
    st = state()
    labels = st.get("labels") or {}
    if not labels:
        return None, 0.0
    label, entry = max(labels.items(), key=lambda kv: kv[1].get("weight", 0.0))
    return label, float(entry.get("weight", 0.0))


def mood() -> float:
    """Coarse pleasant/unpleasant number in -1..1. Diagnostic only."""
    return max(-1.0, min(1.0, state().get("valence_sum", 0.0) / 6.0))


# ── The one string the model actually reads ───────────────────────────────────

# The corroboration bar. One strong, unhedged signal ("stressed", "exhausted")
# clears it. Two weak ones ("tired", "still broken", "again") together clear it,
# which is the whole point: the bar is set so that a single ambiguous word can
# never move how the assistant speaks to someone.
THRESHOLD = 1.0

# Which labels are unreliable from a single occurrence, because their stems have
# common non-emotional uses in ordinary sentences. Derived from the table rather
# than restated, so adding a signal cannot forget to classify it.
_NEEDS_CORROBORATION = {
    stem for stem, (_l, _v, _w, corr) in SIGNALS.items() if corr
}

_GUIDANCE: dict[str, str] = {
    "distress": (
        "The user sounds strained. Do not diagnose it, name it, or ask about "
        "it in those words. Slow down, cut the length, get the practical thing "
        "handled first, and only then leave space. One acknowledgement, in "
        "your own words, no more."
    ),
    "frustration": (
        "The user sounds frustrated, and most likely at the task rather than at "
        "anyone. Fix the thing or say plainly what is blocking it. Do not "
        "sympathise at length — being told 'I understand your frustration' while "
        "nothing changes is the single most irritating response available to you."
    ),
    "fatigue": (
        "The user sounds tired. Be brief and concrete. Do not suggest rest, "
        "sleep or a break unless they raise it — being managed is worse than "
        "being helped."
    ),
    "confusion": (
        "The user is not following. Assume your last answer was the problem, not "
        "their question. Re-answer more plainly and shorter, and do not ask them "
        "to clarify what you failed to explain."
    ),
    "urgency": (
        "The user is in a hurry. Answer first, in the first sentence, and drop "
        "everything that is not needed to act. No preamble, no reassurance about "
        "how busy things are."
    ),
    "resignation": (
        "The user sounds like they have stopped caring about the outcome. Do not "
        "try to talk them up, and do not point out what is at stake — they have "
        "already weighed it. Just make the next step smaller and easier."
    ),
    "gratitude": (
        "The user thanked you. Take it in one word and move on. Do not return "
        "the compliment, do not say you are happy to help, and do not apologise "
        "for anything."
    ),
    "joy": (
        "The user sounds genuinely happy about something. Let it land — react "
        "with real interest, one line, then get on with it. Do not flatten it "
        "into a status update."
    ),
    "relief": (
        "Something the user wanted to be over is over. Acknowledge it in half a "
        "sentence. Do not recap the problem back at them."
    ),
}


def guidance(now: float | None = None) -> str:
    """One short paragraph for the system prompt, or '' when nothing qualifies.

    This is deliberately stingy. It returns text only when a reading has both
    survived decay AND cleared the corroboration bar, because a prompt line that
    says 'the user seems stressed' when they are not makes every future reply
    sound like a performance.
    """
    now = now or time.time()
    st = _decay(_load(), now)

    # A push-back ("stop doing that", "don't read me") suspends all of this.
    if float(st.get("mutes_until", 0.0)) > now:
        return ""

    labels = st.get("labels") or {}
    if not labels:
        return ""

    label, entry = max(labels.items(), key=lambda kv: kv[1].get("weight", 0.0))
    weight = float(entry.get("weight", 0.0))
    count = int(entry.get("n", 0))
    if weight < THRESHOLD:
        return ""

    # A label built only out of ambiguous stems has to have been said in more
    # than one message before it is allowed to change how the assistant speaks.
    stems_for_label = {s for s, sig in SIGNALS.items() if sig[0] == label}
    if stems_for_label <= _NEEDS_CORROBORATION and count < 2:
        return ""

    hint = _GUIDANCE.get(label)
    if not hint:
        return ""

    # Prefer the more urgent of two simultaneous readings: distress outranks
    # frustration outranks everything pleasant, because being upbeat at someone
    # who is having a bad time is the expensive error.
    others = [l for l, e in labels.items()
              if l != label and float(e.get("weight", 0.0)) >= THRESHOLD]
    for extra in ("distress", "frustration", "resignation", "fatigue", "urgency"):
        if extra in others:
            hint = _GUIDANCE[extra] + " " + hint
            break

    return (f"[HOW THE USER SOUNDS RIGHT NOW] {hint} "
            f"(read from what they have said recently; it is your impression, "
            f"not a fact about them, and it may be wrong.)")


def mute(seconds: float = 3600.0) -> None:
    """Stop attuning for a while — call this when the user objects to it."""
    st = _load()
    st["mutes_until"] = time.time() + seconds
    st["labels"] = {}
    _save(st)


def unmute() -> None:
    st = _load()
    st["mutes_until"] = 0.0
    _save(st)


def muted() -> bool:
    return float(_load().get("mutes_until", 0.0)) > time.time()


def reset() -> None:
    try:
        _load_path().unlink()
    except Exception:
        pass
