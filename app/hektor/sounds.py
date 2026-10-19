"""Hektor's little noises: chirps, bloops and trills, made up on the spot so no two are the same.

Hektor makes sounds while idle (to show it's alive) and to mark what happens in a
conversation: a chirp when it wakes up, a blip when it got your question, a sound for each mood.
Everything is synthesized here: a few harmonics, pitch slides, vibrato and short envelopes.
"""
import logging
import random
import subprocess
import time
import wave
from collections.abc import Callable

import numpy as np

from .config import RUNTIME, Conf

log = logging.getLogger(__name__)
RATE = 24000


# --- synthesis -----------------------------------------------------------------------------------

def _contour(f0: float, f1: float, dur: float, glide: str) -> np.ndarray:
    x = np.arange(max(1, int(dur * RATE))) / RATE / max(dur, 1e-6)
    if glide == "exp":
        return f0 * (f1 / f0) ** x
    if glide == "ease":  # most of the slide at the end, like a question
        return f0 + (f1 - f0) * x ** 3
    return f0 + (f1 - f0) * x


def note(f0: float, f1: float | None = None, dur: float = 0.08, vol: float = 1.0, shape: str = "voice",
         vib: tuple[float, float] = (0, 0), attack: float = 0.006, release: float = 0.03,
         glide: str = "exp", path: list[tuple[float, float, str]] | None = None) -> np.ndarray:
    """One tone sliding from f0 to f1 Hz. `vib` = (rate Hz, depth as a fraction of the pitch).

    `path` continues the slide without a break: (to Hz, seconds, glide) steps after f0 → f1.
    """
    f1 = f0 if f1 is None else f1
    parts = [_contour(f0, f1, dur, glide)]
    for to, secs, how in path or []:
        parts.append(_contour(parts[-1][-1], to, secs, how))
    freq = np.concatenate(parts)
    n = len(freq)
    t = np.arange(n) / RATE
    if vib[0]:
        freq = freq * (1 + vib[1] * np.sin(2 * np.pi * vib[0] * t))
    phase = 2 * np.pi * np.cumsum(freq) / RATE
    if shape == "voice":    # soft and round, a bit of buzz
        y = np.sin(phase) + 0.35 * np.sin(2 * phase) + 0.12 * np.sin(3 * phase)
    elif shape == "buzz":   # square-ish, grumbly
        y = np.tanh(3 * np.sin(phase)) + 0.3 * np.sin(2 * phase)
    else:
        y = np.sin(phase)
    env = np.ones(n)
    a, r = min(n, int(attack * RATE)), min(n, int(release * RATE))
    if a:
        env[:a] = np.linspace(0, 1, a)
    if r:
        env[-r:] *= np.linspace(1, 0, r) ** 2
    return vol * y / 1.2 * env


def gap(dur: float) -> np.ndarray:
    return np.zeros(int(dur * RATE))


def noise(dur: float, vol: float = 1.0, smooth: int = 40) -> np.ndarray:
    """Soft filtered noise, for breathing."""
    n = int(dur * RATE)
    y = np.convolve(np.random.randn(n + smooth), np.ones(smooth) / smooth, "valid")[:n]
    env = np.sin(np.linspace(0, np.pi, n)) ** 2
    return vol * y / (np.abs(y).max() + 1e-9) * env


def seq(*parts: np.ndarray) -> np.ndarray:
    return np.concatenate(parts)


# --- the sounds ----------------------------------------------------------------------------------
# Each takes a random generator and returns samples; pitches and timings vary a little every time.

def _wake(r: random.Random) -> np.ndarray:        # "bwee-dip!": I'm listening
    f = r.uniform(850, 1050)
    return seq(note(f * 0.75, f, 0.07), gap(0.02), note(f * 1.3, f * 1.55, 0.09, release=0.05))


def _ack(r: random.Random) -> np.ndarray:         # "bip-bup": got it, thinking
    f = r.uniform(900, 1100)
    return seq(note(f, dur=0.045), gap(0.03), note(f * 0.75, dur=0.06, vol=0.8))


def _shutter(r: random.Random) -> np.ndarray:     # "tchk-tchk": a camera taking a picture
    click = lambda: noise(0.022, vol=0.9, smooth=2)
    return seq(click(), gap(r.uniform(0.07, 0.09)), click())


def _dismiss(r: random.Random) -> np.ndarray:     # "pwoo": nothing heard, never mind
    f = r.uniform(700, 850)
    return note(f * 1.2, f * 0.55, r.uniform(0.2, 0.28), vol=0.7, release=0.08)


def _happy(r: random.Random) -> np.ndarray:       # quick rising arpeggio ending in a trill
    f = r.uniform(600, 760)
    steps = [1, 1.26, 1.5] if r.random() < 0.5 else [1, 1.33, 1.5, 1.68]
    notes = [x for s in steps for x in (note(f * s, dur=r.uniform(0.045, 0.06)), gap(0.012))]
    return seq(*notes, note(f * 2, dur=0.16, vib=(22, 0.04), release=0.07))


def _sad(r: random.Random) -> np.ndarray:         # slow sigh down
    f = r.uniform(520, 620)
    return note(f, f * 0.9, 0.18, vol=0.65, vib=(5, 0.03), release=0.15, path=[(f * 0.5, 0.5, "exp")])


def _surprised(r: random.Random) -> np.ndarray:   # whoop up, then a high blip
    f = r.uniform(380, 460)
    return seq(note(f, f * 3.6, 0.17, glide="ease"), gap(0.03), note(f * 3.8, dur=0.05, vol=0.8))


def _confused(r: random.Random) -> np.ndarray:    # "hm-m?": up, down, up like a question
    f = r.uniform(480, 560)
    return note(f, f * 1.3, 0.12, glide="lin", release=0.05, path=[(f * 1.05, 0.1, "lin"), (f * 1.6, 0.16, "ease")])


def _angry(r: random.Random) -> np.ndarray:       # low buzzy grumble, twice
    f = r.uniform(140, 175)
    return seq(note(f * 1.2, f, 0.16, shape="buzz", vib=(28, 0.06), vol=0.8), gap(0.05),
               note(f * 1.1, f * 0.85, 0.24, shape="buzz", vib=(24, 0.08), vol=0.8, release=0.08))


def _yawn(r: random.Random) -> np.ndarray:        # going to sleep
    f = r.uniform(500, 580)
    return note(f, f * 1.35, 0.35, vol=0.6, glide="lin", vib=(4, 0.02), release=0.3, path=[(f * 0.55, 0.7, "exp")])


def _wakeup(r: random.Random) -> np.ndarray:      # waking up: a stretch and a chirp
    f = r.uniform(420, 500)
    return seq(note(f, f * 1.8, 0.3, vol=0.6, glide="ease"), gap(0.05), note(f * 2.4, dur=0.05), gap(0.02),
               note(f * 2.9, dur=0.06))


def _snore(r: random.Random) -> np.ndarray:       # soft breath in, low hum out
    return seq(noise(0.9, vol=0.25), note(r.uniform(110, 130), None, 0.8, vol=0.15, shape="sine",
                                          attack=0.2, release=0.4))


# idle: small "I'm here" noises
def _bip(r: random.Random) -> np.ndarray:
    return note(r.uniform(900, 1500), dur=r.uniform(0.04, 0.07), vol=0.7)


def _bidoo(r: random.Random) -> np.ndarray:
    f = r.uniform(600, 1000)
    return seq(note(f, dur=0.06, vol=0.7), gap(0.02), note(f * r.choice([0.75, 1.25, 1.5]), dur=0.09, vol=0.7))


def _trill(r: random.Random) -> np.ndarray:
    f = r.uniform(800, 1200)
    return note(f, f * r.uniform(0.85, 1.2), r.uniform(0.18, 0.3), vol=0.6, vib=(r.uniform(18, 28), 0.06))


def _murmur(r: random.Random) -> np.ndarray:      # a little robot mumble to itself
    f = r.uniform(380, 520)
    parts = []
    for _ in range(r.randint(3, 6)):
        parts += [note(f * r.uniform(0.8, 1.5), f * r.uniform(0.8, 1.5), r.uniform(0.04, 0.09), vol=0.5,
                       glide="lin", release=0.015), gap(r.uniform(0.0, 0.03))]
    return seq(*parts)


def _hmm(r: random.Random) -> np.ndarray:
    f = r.uniform(300, 380)
    return note(f, f * r.choice([0.8, 1.25]), r.uniform(0.25, 0.35), vol=0.5, vib=(6, 0.02), release=0.1)


SOUNDS: dict[str, Callable[[random.Random], np.ndarray]] = {
    "wake": _wake, "ack": _ack, "dismiss": _dismiss, "shutter": _shutter,
    "happy": _happy, "sad": _sad, "surprised": _surprised, "confused": _confused, "angry": _angry,
    "yawn": _yawn, "wakeup": _wakeup, "snore": _snore,
    "bip": _bip, "bidoo": _bidoo, "trill": _trill, "murmur": _murmur, "hmm": _hmm,
}
IDLE = ["bip", "bidoo", "trill", "murmur", "hmm"]


def make(name: str, rng: random.Random | None = None) -> np.ndarray:
    return SOUNDS[name](rng or random.Random())


def write_wav(path, samples: np.ndarray, volume: float = 1.0) -> None:
    data = (np.clip(samples * volume, -1, 1) * 32767).astype("<i2").tobytes()
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data)


# --- playing -------------------------------------------------------------------------------------

class Sounds:
    """Plays the noises, and the idle ones on its own: call update() every frame."""

    def __init__(self, conf: Conf, enabled: bool = True) -> None:
        from .tts import pw_play  # through the echo canceller, like the voice
        self._play = pw_play
        self.enabled = enabled
        self.volume = conf.num("SOUND_VOLUME")
        idle = conf.get("IDLE_SOUNDS", "").split()
        self.idle = (float(idle[0]), float(idle[1])) if len(idle) == 2 and float(idle[1]) > 0 else None
        self.snore = conf.yes("SLEEP_SOUNDS")
        self.next_idle = time.monotonic() + (self._idle_gap() if self.idle else 0)
        self.n = 0
        RUNTIME.mkdir(parents=True, exist_ok=True)

    def _idle_gap(self) -> float:
        return random.uniform(*self.idle)

    def play(self, name: str, wait: bool = False) -> None:
        if not self.enabled or name not in SOUNDS:
            return
        log.debug("sound: %s", name)
        self.n = (self.n + 1) % 8  # a few files, so a new sound doesn't overwrite one still playing
        path = RUNTIME / f"sound-{self.n}.wav"
        write_wav(path, make(name), self.volume)
        proc = self._play(path)
        if wait:
            proc.wait()

    def update(self, state: str) -> str | None:
        """Make an idle noise now and then; returns its name so the face can react."""
        now = time.monotonic()
        if state not in ("idle", "sleeping"):
            # just talked: wait a full gap before the next idle noise
            self.next_idle = now + (self._idle_gap() if self.idle else 0)
            return None
        if now < self.next_idle:
            return None
        if state == "sleeping":
            self.next_idle = now + 4.5
            if self.snore:
                self.play("snore")
                return "snore"
            return None
        if not self.idle:
            return None
        self.next_idle = now + self._idle_gap()
        name = random.choice(IDLE)
        self.play(name)
        return name
