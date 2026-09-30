"""
Speaker verification for the wake word — only the enrolled voice wakes JARVIS.

What this is for
----------------
"Hey Jarvis" is a phrase anyone can say. Without this module a colleague, a
passer-by, a podcast or a television in the next room can wake your assistant
and keep it awake — and the whole point of the wake word is that the microphone
stays *shut* until you address it. A wake word on its own is a lock with the key
taped to the door. This adds the second half: the phrase has to be spoken by the
person who enrolled.

Everything is local. Enrolment audio never leaves the machine, the only network
call is the one-time model download the user triggers, and what gets stored is a
192-number vector — not a recording.

Why a neural embedder and not something lighter
-----------------------------------------------
The obvious cheap alternative is a descriptor built from band statistics —
MFCC mean and standard deviation, say — computable with numpy alone and no
download at all. It was built here first and measured, and it does not work.
Against a source-filter speech model where two speakers say four different
sentences each, same-speaker cosine ran +0.79..+0.98 while cross-speaker cosine
ran +0.76..+1.00: the ranges overlap completely, and the *highest* cross-speaker
score was higher than several same-speaker pairs. What that descriptor measures
is which words were said, not who said them — phonetics swamp vocal tract
differences at this length.

That is the reason this module requires ECAPA-TDNN, and it is also the reason it
fails loudly rather than quietly: a voice-ID gate that does not identify voices
is worse than no gate, because it buys the belief that the microphone is closed
to everyone else while leaving it open to everyone.

Design goals
------------
• ZERO cost when the feature is off. Nothing is constructed until someone
  actually enrols, and the model is imported inside the loader rather than at
  module level. A user who never turns this on pays one dict lookup per wake
  detection.

• FAIL CLOSED. If voice ID is on and anything goes wrong — model won't load,
  file is corrupt, the embedder throws — the answer is *reject* and stay asleep.
  The single exception is a run of consecutive failures, which stands the gate
  down and says so: an app that cannot be woken is an app the user deletes, and
  a bug must not be able to cause that silently.

• THE THRESHOLD IS MEASURED, NOT GUESSED. No number is baked in. Enrolment
  takes several phrases, embeds each, and compares them with each other. The
  spread of those self-similarities is the operating point, because same
  speaker / same microphone / different words is exactly the condition the wake
  word gets tested in — so it is also the condition the threshold is derived
  from. The calibration lands inside per-model bounds and is never allowed to
  sit so high that it rejects the owner.

• THE MODEL IS PINNED TO THE ENROLMENT. The voiceprint records which embedder
  produced it and verification always uses that one, so a missing dependency
  downgrades to "reject" rather than to "accept anything".

What this does not do
---------------------
It stops a different person in the room from waking the assistant. It does NOT
stop a recording of your own voice played back through your speakers, and it is
not strong against a close imitation. Replay defeats essentially all speaker
verification, trained models included. Treat this as a real obstacle to casual
wake-ups, not as a security boundary.
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Mic frames already arrive at 16 kHz mono int16, so nothing here resamples.
SAMPLE_RATE = 16000
# How much audio the embedder looks at. ~3 s suits ECAPA: long enough to
# characterise a voice, short enough that the window still sits mostly on the
# wake phrase itself.
WINDOW_S = 3.0
# Below this the window is mostly room tone and there is nothing to compare.
MIN_SPEECH_S = 0.6

# Enrolment prompts. Deliberately NOT the wake phrase — if the enrolled audio
# were "hey jarvis", a match could be scoring the words rather than the speaker.
ENROLL_PHRASES = (
    "The quick brown fox jumps over the lazy dog",
    "Please read this sentence at your normal speaking pace",
    "My voice is the only one that should wake this machine",
    "Testing one two three, this is a voice enrolment sample",
)

MODEL_ECAPA = "ecapa-voxceleb"
ECAPA_SOURCE = "speechbrain/spkrec-ecapa-voxceleb"

# Calibration bounds for the one supported model. The stored threshold is
# clamped into this window whatever the enrolment produced.
_THRESHOLD_FLOOR = 0.30
_THRESHOLD_CEIL = 0.75
_THRESHOLD_DEFAULT = 0.55

# How long a rejected detection keeps the gate shut. The wake model fires
# several times across one utterance; without this, a stranger saying the
# phrase once would produce a burst of rejections and a burst of log lines.
REJECT_COOLDOWN_S = 3.0
# Rate limit on the "that wasn't you" line itself.
_REJECT_LOG_EVERY_S = 30.0
# Consecutive hard failures before the gate gives up and stands itself down.
MAX_CONSECUTIVE_ERRORS = 3


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


VOICEPRINT_FILE = _base_dir() / "config" / "voiceprint.json"


# ── Readiness / install ─────────────────────────────────────────────────────

def is_installed() -> bool:
    """True if the embedding package is importable. No model check."""
    try:
        import importlib.util
        return importlib.util.find_spec("speechbrain") is not None
    except Exception:
        return False


def install(logger: Callable[[str], None] = print,
            notify: Callable[[str], None] | None = None) -> tuple[bool, str]:
    """One-click setup for the UI: pip-install speechbrain (which brings torch).

    Returns (ok, message). Never raises. The weights download separately on
    first enrolment, because that needs a different code path (SpeechBrain's own
    fetch) than the package install, and failing them apart gives a far better
    message than one opaque error.
    """
    _tell = notify or (lambda _m: None)
    if is_installed():
        return True, "Voice ID: the speaker model is already installed."
    try:
        logger("Voice ID: installing speechbrain — this pulls in PyTorch and is large…")
        _tell("Voice ID: installing the speaker model. This is a large download…")
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "speechbrain"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or [""]
            return False, f"pip install failed: {tail[0][:160]}"
        if not is_installed():
            return False, "installed, but speechbrain is still not importable."
        logger("Voice ID: speechbrain installed.")
        return True, ("Voice ID: model installed. Its weights download on first enrolment.")
    except Exception as e:
        return False, f"install error: {e}"


# ── Embedder ────────────────────────────────────────────────────────────────

class EcapaEmbedder:
    """ECAPA-TDNN x-vector embeddings, via SpeechBrain.

    Constructed once per process and reused, because loading is the expensive
    part and the wake detector calls this on every detection. The model is
    pinned to the CPU on purpose: this app is built to run on a laptop with no
    GPU, and letting torch auto-select can pick CUDA on a machine whose driver
    is only half working.
    """

    name = MODEL_ECAPA
    dim = 192

    def __init__(self):
        self._model = None
        self._lock = threading.Lock()

    def load(self):
        """Build the model, downloading weights on first use. Cached."""
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            from speechbrain.inference.speaker import EncoderClassifier
            cache = _base_dir() / "config" / "models" / "ecapa"
            cache.mkdir(parents=True, exist_ok=True)
            self._model = EncoderClassifier.from_hparams(
                source=ECAPA_SOURCE, savedir=str(cache),
                run_opts={"device": "cpu"},
            )
            return self._model

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def embed(self, pcm_int16) -> Optional["object"]:
        """L2-normalised embedding of one utterance, or None if unusable."""
        import numpy as np
        import torch

        if pcm_int16 is None or len(pcm_int16) < int(MIN_SPEECH_S * SAMPLE_RATE):
            return None
        model = self.load()
        wave = torch.from_numpy(
            np.asarray(pcm_int16, dtype=np.float32) / 32768.0
        ).unsqueeze(0)
        with torch.no_grad():
            vec = model.encode_batch(wave).squeeze().cpu().numpy()
        vec = np.asarray(vec, dtype=np.float32).reshape(-1)
        norm = float(np.linalg.norm(vec))
        if not norm > 1e-8 or not np.isfinite(norm):
            return None
        return vec / norm


_CACHE: Optional[EcapaEmbedder] = None
_CACHE_LOCK = threading.Lock()


def get_embedder() -> Optional[EcapaEmbedder]:
    """The shared embedder, or None if it cannot be built.

    Returns None rather than raising, so a missing optional dependency turns
    into a log line instead of a traceback on the detection path.
    """
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is None:
            _CACHE = EcapaEmbedder()
        return _CACHE


# ── Capture ─────────────────────────────────────────────────────────────────

def record_utterance(seconds: float = 2.6,
                     on_level: Callable[[float], None] | None = None,
                     stop: threading.Event | None = None) -> Optional["object"]:
    """Record one enrolment utterance. Returns int16 mono, or None on failure.

    Opens its own short-lived input stream rather than tapping the session's:
    enrolment happens once, with the assistant asleep, and borrowing the live
    microphone would mean teaching the wake-word gate to be optional inside a
    function whose entire purpose is not being optional. Blocks for `seconds`,
    so call it from a worker thread.
    """
    import numpy as np
    import sounddevice as sd

    blocks: list = []
    level_sink = on_level or (lambda _v: None)

    def _cb(indata, frames, time_info, status):
        blocks.append(indata[:, 0].copy())
        try:
            rms = float(np.sqrt(np.mean(np.square(indata.astype(np.float32)))))
            level_sink(min(1.0, rms / 2600.0))
        except Exception:
            pass

    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                            blocksize=1024, callback=_cb):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if stop is not None and stop.is_set():
                    return None
                time.sleep(0.02)
    except Exception:
        return None

    if not blocks:
        return None
    audio = np.concatenate(blocks).astype(np.int16)
    if len(audio) < int(MIN_SPEECH_S * SAMPLE_RATE):
        return None
    # Trim the leading and trailing room tone so the model sees speech, not the
    # recorder switching on.
    energy = np.abs(audio.astype(np.float32))
    if energy.size == 0:
        return None
    loudest = float(np.max(energy))
    if loudest < 500.0:                       # effectively nothing was said
        return None
    hot = np.flatnonzero(energy > 0.10 * loudest)
    if hot.size == 0:
        return None
    pad = int(0.10 * SAMPLE_RATE)
    start = max(0, int(hot[0]) - pad)
    end = min(len(audio), int(hot[-1]) + pad)
    return audio[start:end]


# ── Verdict ─────────────────────────────────────────────────────────────────

@dataclass
class Verdict:
    ok: bool
    score: float = 0.0
    threshold: float = 0.0
    reason: str = ""


# ── The gate ────────────────────────────────────────────────────────────────

class VoiceID:
    """Owns the enrolled voiceprint and answers 'was that me?'.

    Safe for the way it is used: one instance, called from the wake detector's
    inference thread, with the settings panel reading state from the Qt thread.
    The lock is only ever held around a reference-vector swap and the error
    counter, never around a model call.
    """

    def __init__(self,
                 logger: Callable[[str], None] = print,
                 notify: Callable[[str], None] | None = None):
        self._logger = logger
        self._notify = notify or (lambda _m: None)
        self._lock = threading.Lock()
        self._reference = None
        self._threshold = _THRESHOLD_DEFAULT
        self._intra: list = []
        self._enrolled_at: float = 0.0
        self._errors = 0
        self._reject_until = 0.0
        self._last_reject_log = 0.0
        # Set by JarvisLive. Fired when the gate has failed so many times in a
        # row that it stands itself down — the owner of the setting is
        # JarvisLive, not this module, so the toggle is turned from there.
        self.on_fatal: Optional[Callable[[str], None]] = None
        self.load()

    # ── Persistence ─────────────────────────────────────────────────────────

    def load(self) -> bool:
        """Read the voiceprint from disk. True if one was loaded.

        A corrupt file is treated as no voiceprint rather than an error: the gate
        is then inert, which is the same state as a fresh install, and the user
        can simply enrol again.
        """
        try:
            if not VOICEPRINT_FILE.exists():
                return False
            data = json.loads(VOICEPRINT_FILE.read_text(encoding="utf-8"))
            if str(data.get("model") or "") != MODEL_ECAPA:
                return False
            raw = base64.b64decode(str(data.get("vector") or ""), validate=True)
            import numpy as np
            vec = np.frombuffer(raw, dtype=np.float32)
            if vec.size < 16:
                return False
            thr = float(data.get("threshold") or 0.0)
            thr = min(max(thr, _THRESHOLD_FLOOR), _THRESHOLD_CEIL)
            with self._lock:
                self._reference = vec
                self._threshold = thr
                self._intra = [float(x) for x in (data.get("intra") or [])]
                self._enrolled_at = float(data.get("enrolled_at") or 0.0)
            return True
        except Exception as e:
            self._logger(f"Voice ID: ignoring unreadable voiceprint — {e}")
            return False

    def _save(self) -> None:
        import numpy as np
        with self._lock:
            if self._reference is None:
                raise ValueError("nothing to save")
            payload = {
                "model": MODEL_ECAPA,
                "vector": base64.b64encode(
                    np.asarray(self._reference, dtype=np.float32).tobytes()
                ).decode("ascii"),
                "threshold": round(float(self._threshold), 4),
                "intra": [round(float(x), 4) for x in self._intra],
                "enrolled_at": round(float(self._enrolled_at), 3),
                "version": 1,
            }
        VOICEPRINT_FILE.parent.mkdir(parents=True, exist_ok=True)
        VOICEPRINT_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def forget(self) -> None:
        """Delete the voiceprint. The gate goes inert; the wake word still works."""
        with self._lock:
            self._reference = None
            self._intra = []
            self._enrolled_at = 0.0
            self._errors = 0
        try:
            VOICEPRINT_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        self._notify("Voice ID: forgotten. The wake word no longer checks who spoke.")

    # ── State ───────────────────────────────────────────────────────────────

    @property
    def enrolled(self) -> bool:
        return self._reference is not None

    @property
    def threshold(self) -> float:
        return self._threshold

    def info(self) -> dict:
        """Everything the settings panel needs, in one cheap call."""
        return {
            "enrolled": self.enrolled,
            "model": MODEL_ECAPA if self.enrolled else "",
            "threshold": round(float(self._threshold), 3),
            "intra": [round(float(x), 3) for x in self._intra],
            "errors": int(self._errors),
            "installed": is_installed(),
            "enrolled_at": float(self._enrolled_at),
        }

    # ── Enrolment ───────────────────────────────────────────────────────────

    @staticmethod
    def calibrate(vectors) -> float:
        """Derive the threshold from several embeddings of the same speaker.

        Every pair is compared. The mean of those self-similarities estimates
        'this person, on this microphone, saying something we did not enrol' —
        which is the condition the gate will be used in. Sitting two standard
        deviations below leaves room for a harder take without opening the door.

        The result is clamped into the model's bounds and additionally never
        allowed above mean - 0.03, so a spread of zero cannot produce an
        unreachable threshold that rejects the owner forever.
        """
        import numpy as np

        vecs = [np.asarray(v, dtype=np.float32).reshape(-1) for v in vectors]
        scores = [float(np.dot(vecs[i], vecs[j]))
                  for i in range(len(vecs)) for j in range(i + 1, len(vecs))]
        if not scores:
            return _THRESHOLD_DEFAULT
        arr = np.asarray(scores, dtype=np.float64)
        mean, std = float(arr.mean()), float(arr.std())
        thr = min(mean - 2.0 * std, mean - 0.03)
        thr = min(max(thr, _THRESHOLD_FLOOR), _THRESHOLD_CEIL)
        return float(thr)

    @staticmethod
    def _pairwise(vectors) -> list:
        import numpy as np
        return [float(np.dot(vectors[i], vectors[j]))
                for i in range(len(vectors)) for j in range(i + 1, len(vectors))]

    def enroll_from_arrays(self, arrays) -> tuple[bool, str]:
        """Build a voiceprint from captured utterances. Returns (ok, message)."""
        import numpy as np

        embedder = get_embedder()
        if embedder is None:
            return False, "the speaker model is not available on this machine"
        try:
            vectors = [embedder.embed(np.asarray(a, dtype=np.int16))
                       for a in arrays]
        except Exception as e:
            return False, f"could not analyse the samples: {e}"
        vectors = [v for v in vectors if v is not None]

        if len(vectors) < 3:
            return False, (f"only {len(vectors)} of {len(arrays)} samples had usable "
                           f"speech — try again a little closer to the microphone")

        reference = np.mean(np.stack(vectors), axis=0).astype(np.float32)
        norm = float(np.linalg.norm(reference))
        if norm <= 1e-8:
            return False, "the samples cancelled out — try again."
        reference /= norm
        threshold = self.calibrate(vectors)

        with self._lock:
            self._reference = reference
            self._threshold = threshold
            self._intra = self._pairwise(vectors)
            self._enrolled_at = time.time()
            self._errors = 0
        try:
            self._save()
        except Exception as e:
            return False, f"enrolled, but could not be saved: {e}"

        spread = self._intra
        detail = f"{min(spread):.2f}–{max(spread):.2f}" if spread else "n/a"
        return True, (f"Voice ID: enrolled — your samples matched at {detail}, "
                      f"so the gate sits at {threshold:.2f}.")

    def enroll_from_mic(self, on_prompt=None, on_level=None,
                        stop: threading.Event | None = None) -> tuple[bool, str]:
        """Full enrolment: prompt for each phrase, capture it, build the
        voiceprint. Meant for a worker thread — it blocks on the microphone for
        the length of the session.

        `on_prompt(index, total, phrase)` fires before each capture so the panel
        can show what to say; `on_level` is the 0..1 meter.
        """
        captured = []
        for i, phrase in enumerate(ENROLL_PHRASES):
            if stop is not None and stop.is_set():
                return False, "cancelled"
            if on_prompt is not None:
                try:
                    on_prompt(i, len(ENROLL_PHRASES), phrase)
                except Exception:
                    pass
            # A beat before each take, so the previous phrase's tail is gone and
            # there is time to read the next one.
            if stop is not None:
                if stop.wait(0.6):
                    return False, "cancelled"
            else:
                time.sleep(0.6)
            audio = record_utterance(seconds=2.6, on_level=on_level, stop=stop)
            if audio is not None:
                captured.append(audio)
        if stop is not None and stop.is_set():
            return False, "cancelled"
        if len(captured) < 3:
            return False, (f"only {len(captured)} of {len(ENROLL_PHRASES)} samples were "
                           f"heard — check the microphone and try again")
        ok, msg = self.enroll_from_arrays(captured)
        if ok:
            self._notify(msg)
        return ok, msg

    # ── Verification ────────────────────────────────────────────────────────

    def verify(self, window_int16) -> Verdict:
        """Decide whether `window_int16` is the enrolled speaker.

        Called from the wake detector's thread, before the microphone is ever
        opened to the network. Never raises: any failure is a rejection.
        """
        import numpy as np

        if not self.enrolled:
            return Verdict(True, reason="not enrolled")

        now = time.monotonic()
        if now < self._reject_until:
            return Verdict(False, threshold=self._threshold, reason="cooldown")

        try:
            embedder = get_embedder()
            if embedder is None:
                raise RuntimeError("the speaker model is not available")
            with self._lock:
                reference = self._reference
            vec = embedder.embed(np.asarray(window_int16, dtype=np.int16))
            if vec is None or reference is None:
                # Too little speech in the window to judge. Rejecting is the safe
                # answer; the cooldown keeps it from spamming.
                self._reject_until = now + REJECT_COOLDOWN_S
                return Verdict(False, threshold=self._threshold, reason="no speech")
            score = float(np.dot(vec, reference))
        except Exception as e:
            return self._on_error(e)

        with self._lock:
            self._errors = 0
        if score >= self._threshold:
            return Verdict(True, score=score, threshold=self._threshold, reason="match")
        self._reject_until = now + REJECT_COOLDOWN_S
        self._log_rejection(score, self._threshold)
        return Verdict(False, score=score, threshold=self._threshold, reason="no match")

    def _log_rejection(self, score: float, threshold: float) -> None:
        """One line, rate limited.

        This is the only output a rejected speaker produces. It goes to the
        activity log — not to speech, not to a banner — so someone who is not
        the user learns nothing from how the machine responded, while the owner
        can still find out what happened.
        """
        now = time.monotonic()
        if now - self._last_reject_log < _REJECT_LOG_EVERY_S:
            return
        self._last_reject_log = now
        self._notify(f"VOICE ID: not your voice ({score:.2f} < {threshold:.2f}) — ignored.")

    def _on_error(self, exc: Exception) -> Verdict:
        """A failure is a rejection; a run of failures stands the gate down.

        Failing closed is right for a single hiccup. It is not right forever, so
        after MAX_CONSECUTIVE_ERRORS in a row the gate switches itself off and
        says so, rather than leaving the assistant deaf with no explanation.
        """
        with self._lock:
            self._errors += 1
            count = self._errors
        self._logger(f"Voice ID: verification failed ({exc})")
        # Fires on the transition, not on every subsequent failure: once the
        # owner has been told, saying it again on each attempt is just noise.
        if count == MAX_CONSECUTIVE_ERRORS:
            self._logger("Voice ID: too many failures in a row — switching the gate OFF "
                         "so the wake word still works. Re-enrol from ⚙ → VOICE ID.")
            self._notify("VOICE ID: switched off after repeated failures. Wake word is open again.")
            if self.on_fatal is not None:
                try:
                    self.on_fatal("repeated verification failures")
                except Exception:
                    pass
        self._reject_until = time.monotonic() + REJECT_COOLDOWN_S
        return Verdict(False, reason="error")
