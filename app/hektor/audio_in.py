"""Microphone: one PipeWire stream of 100 ms chunks, the wake word, and recording until you go quiet."""
import array
import bisect
import json
import logging
import math
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

from .config import Conf

log = logging.getLogger(__name__)

RATE = 16000
CHUNK = RATE // 10  # 100 ms, as samples
CHUNK_S = 0.1


def rms(data: bytes) -> float:
    samples = array.array("h", data)
    return math.sqrt(sum(s * s for s in samples) / max(len(samples), 1))


VOICE_FRAME = 512  # samples, 32 ms: long enough for two periods of a low voice


def voicing(data: bytes) -> float:
    """How voiced a chunk is, 0-1: the strongest pitch-like repetition (70-400 Hz) in any of three short
    frames in it. Vowels give ~0.6-1; hiss, fans, clicks and thumps ~0.2-0.45."""
    x = np.frombuffer(data, np.int16).astype(np.float32)
    lags = np.arange(RATE // 400, RATE // 70)
    best = 0.0
    for start in range(0, len(x) - VOICE_FRAME + 1, max(1, (len(x) - VOICE_FRAME) // 2)):
        f = x[start:start + VOICE_FRAME]
        f = f - f.mean()
        spec = np.fft.rfft(f, 2 * VOICE_FRAME)
        ac = np.fft.irfft(spec * np.conj(spec))[:VOICE_FRAME]
        if ac[0] > 0:  # normalized by how much of the frame overlaps at each lag
            best = max(best, float((ac[lags] / (ac[0] * (VOICE_FRAME - lags) / VOICE_FRAME)).max()))
    return min(best, 1.0)


class HighPass:
    """Cuts hum and rumble below `cutoff` Hz: two 2nd-order Butterworth sections, kept across chunks."""

    def __init__(self, cutoff: float) -> None:
        w0 = 2 * math.pi * cutoff / RATE
        alpha, cos = math.sin(w0) / (2 * 0.7071), math.cos(w0)
        a0 = 1 + alpha
        self.b = ((1 + cos) / 2 / a0, -(1 + cos) / a0, (1 + cos) / 2 / a0)
        self.a = (-2 * cos / a0, (1 - alpha) / a0)
        self.state = [[0.0] * 4, [0.0] * 4]  # x1, x2, y1, y2 per section

    def __call__(self, data: bytes) -> bytes:
        samples = array.array("h", data)
        out = [float(s) for s in samples]
        (b0, b1, b2), (a1, a2) = self.b, self.a
        for st in self.state:
            x1, x2, y1, y2 = st
            for i, x in enumerate(out):
                y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
                x2, x1, y2, y1 = x1, x, y1, y
                out[i] = y
            st[:] = x1, x2, y1, y2
        return array.array("h", (max(-32768, min(32767, round(y))) for y in out)).tobytes()


EC_MIC, EC_CAPTURE, EC_SPEAKER = "hektor_mic", "hektor_ec_capture", "hektor_speaker"  # audio-setup.sh


def pw_dump() -> list[dict]:
    try:
        return json.loads(subprocess.run(["pw-dump"], capture_output=True, text=True).stdout)
    except (OSError, ValueError):
        return []


def pw_node(name: str, objs: list[dict] | None = None) -> int | None:
    """Id of the PipeWire node called `name`, None if there's none."""
    return next((o["id"] for o in (pw_dump() if objs is None else objs)
                 if o.get("type") == "PipeWire:Interface:Node"
                 and o.get("info", {}).get("props", {}).get("node.name") == name), None)


def pick_mic(preferred: list[str]) -> str | None:
    """Pick the microphone: a `preferred` one (node name prefix), any USB mic, the default input
    if it's a real microphone, else the built-in one.

    Bluetooth earbuds in music mode (A2DP) become the default "source" but have no mic, which
    records pure silence.
    """
    objs = pw_dump()
    sources = [o["info"]["props"] for o in objs
               if o.get("type") == "PipeWire:Interface:Node"
               and o.get("info", {}).get("props", {}).get("media.class") == "Audio/Source"
               and o["info"]["props"].get("node.name") != EC_MIC]
    names = [p.get("node.name") for p in sources]
    default = next((m["value"].get("name") for o in objs
                    if o.get("type") == "PipeWire:Interface:Metadata"
                    and o.get("props", {}).get("metadata.name") == "default"
                    for m in o.get("metadata", []) if m.get("key") == "default.audio.source"), None)
    for prefix in preferred:
        match = [n for n in names if n and n.startswith(prefix)]
        if match:
            return match[0]
    usb = [n for n in names if n and n.startswith("alsa_input.usb-")]
    if usb:
        return usb[0]
    if default in names:
        return default
    builtin = [p["node.name"] for p in sources if "Digital Microphone" in p.get("node.description", "")]
    alsa = [n for n in names if n and n.startswith("alsa_input")]
    return (builtin or alsa or [None])[0]


class Mic:
    """Reads the mic on its own thread and hands every 100 ms chunk to `on_chunk`.

    Reopens the stream when it ends (mic unplugged, suspend) and, every 30 s, switches when a
    better mic shows up (docking). With MIC_NOISE_SUPPRESSION and the "Hektor mic" from
    audio-setup.sh, the picked mic is routed through it and that's what gets recorded.
    """

    def __init__(self, conf: Conf, on_chunk: Callable[[bytes], None]) -> None:
        self.preferred = conf["PREFERRED_MICS"].split()
        self.on_chunk = on_chunk
        self.suppress = conf.yes("MIC_NOISE_SUPPRESSION")
        self.highpass = conf.num("MIC_HIGHPASS")
        self.filter = HighPass(self.highpass) if self.highpass > 0 else None
        self.name: str | None = None
        self.cancelled = False  # recording through the echo canceller: Hektor doesn't hear itself
        self.proc: subprocess.Popen | None = None
        self.stopped = False
        threading.Thread(target=self._run, name="mic", daemon=True).start()

    def _open(self) -> None:
        self.name = pick_mic(self.preferred)
        target = self.name
        capture = pw_node(EC_CAPTURE) if self.suppress else None
        self.cancelled = capture is not None
        if capture is not None:
            # point the echo canceller's input at our mic, and record its cleaned-up output
            if self.name:
                subprocess.run(["pw-metadata", str(capture), "target.object", self.name],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            target = EC_MIC
            log.info("recording from %s through the Hektor mic (noise suppressed)", self.name or "the default mic")
        else:
            log.info("recording from %s%s", self.name or "the default mic",
                     "" if not self.suppress else " (no Hektor mic: run audio-setup.sh for noise suppression)")
        if self.filter:
            log.info("mic high-pass filter at %.0f Hz", self.highpass)
        self.proc = subprocess.Popen(
            ["pw-record", *(["--target", target] if target else []),
             "--rate", str(RATE), "--channels", "1", "--format", "s16", "--raw", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True)  # Ctrl+C in the terminal stops us, and we stop it

    def _run(self) -> None:
        self._open()
        last_check = time.monotonic()
        while not self.stopped:
            data = self.proc.stdout.read(CHUNK * 2)
            if len(data) < CHUNK * 2:
                if self.stopped:
                    return
                log.warning("mic stream ended, reopening")
                time.sleep(1)
                self._open()
                continue
            self.on_chunk(self.filter(data) if self.filter else data)
            if time.monotonic() - last_check > 30:
                last_check = time.monotonic()
                if (new := pick_mic(self.preferred)) != self.name:
                    log.info("switching mic to %s", new)
                    self.proc.terminate()

    def stop(self) -> None:
        self.stopped = True
        if self.proc:
            self.proc.terminate()


class WakeWord:
    """Spots the wake phrases with a small offline Vosk model restricted to just those phrases."""

    def __init__(self, conf: Conf) -> None:
        import vosk
        vosk.SetLogLevel(-1)
        self.phrases = [p.strip().lower() for p in conf["WAKE_PHRASES"].split(",") if p.strip()]
        model = conf.path("WAKE_MODEL")
        if not model.is_dir():
            raise FileNotFoundError(f"no wake word model in {model}, run fetch-models.sh")
        self.rec = vosk.KaldiRecognizer(vosk.Model(str(model)), RATE, json.dumps(self.phrases + ["[unk]"]))
        self.last = ""
        log.info("listening for %s", self.phrases)

    def heard(self, data: bytes) -> bool:
        # partial results fire while you're still talking, so "hey hector, what time is it" works in one go
        final = self.rec.AcceptWaveform(data)
        text = json.loads(self.rec.Result() if final else self.rec.PartialResult())
        heard = text.get("partial", text.get("text", ""))
        if heard and heard != self.last:  # what it makes of what it hears, in debug mode
            log.debug("wake spotter %s: %s", "heard" if final else "hearing", heard)
        self.last = "" if final else heard
        if any(p in heard for p in self.phrases):
            self.rec.Reset()
            self.last = ""
            return True
        return False

    def reset(self) -> None:
        self.rec.Reset()


class Recorder:
    """Collects chunks until the speaker goes quiet.

    `add` returns None while recording, then True when something was said, False when nothing was.

    A chunk sounds like talking when it's loud and voiced (VOICED: vowels have a pitch, fans, hiss,
    clicks and thumps don't), and you're talking when most of the last few chunks do (SPEECH_RUN of
    the last SPEECH_RUN + 2). Speech starts that way, ignoring the first moment, when Hektor's own
    "I'm listening" chirp may still be heard. It ends after SILENCE_END seconds without talking, where
    "loud" is measured against the speech itself (SILENCE_SHARE of its typical loudness, if that's
    above the threshold): the room is often louder while you talk than in the quiet moment it was
    measured.
    """

    IGNORE_START = 0.5  # seconds: the wake chirp, not counted as talking
    CHIRP = 4           # chunks of it left out of the audio for whisper, which hears it as a word

    def __init__(self, conf: Conf, noise: float) -> None:
        self.silence_end = conf.num("SILENCE_END")
        self.no_speech = conf.num("NO_SPEECH")
        self.max_len = conf.num("MAX_LEN")
        self.speech_run = conf.int("SPEECH_RUN")
        self.share = conf.num("SILENCE_SHARE")
        self.voiced = conf.num("VOICED")
        self.threshold = max(conf.num("MIN_RMS"), noise * conf.num("NOISE_FACTOR"))
        self.frames: list[bytes] = []
        self.heard, self.quiet, self.t = False, 0.0, 0.0
        self.recent: deque[bool] = deque(maxlen=self.speech_run + 2)  # talking-like, last few chunks
        self.loudest = 0.0
        self.speech: list[float] = []  # levels of the talking-like chunks, sorted, once speech started
        self.ignore_until = self.IGNORE_START
        self.chirp_at = 0  # frame where the chirp starts

    @property
    def quiet_level(self) -> float:
        """Under this it's quiet: the threshold, or a share of the typical speech level if that's higher."""
        if not self.speech:
            return self.threshold
        return max(self.threshold, self.speech[len(self.speech) // 2] * self.share)

    def add(self, data: bytes) -> bool | None:
        self.frames.append(data)
        self.t += CHUNK_S
        level = rms(data)
        self.loudest = max(self.loudest, level)
        talky = level > self.quiet_level and self.t > self.ignore_until and voicing(data) >= self.voiced
        self.recent.append(talky)
        talking = sum(self.recent) >= self.speech_run
        if talky and (self.heard or talking):
            bisect.insort(self.speech, level)
        if not self.heard and talking:
            log.debug("speech started at %.1f s", self.t)
            self.heard = True
        if self.heard:
            self.quiet = 0.0 if talking else self.quiet + CHUNK_S
        if (self.heard and self.quiet >= self.silence_end) or self.t >= self.max_len:
            return self.heard
        if not self.heard and self.t >= self.no_speech:
            return False
        return None

    def prefill(self, frames: list[bytes]) -> None:
        """Start with what was already said (talking over Hektor); the chirp comes after it."""
        self.ignore_until = 0.0
        for data in frames:
            self.add(data)
        self.ignore_until = self.t + self.IGNORE_START
        self.chirp_at = len(self.frames)

    def finish(self) -> bool:
        """Stop now (the talk key): worth transcribing if anything was said or it ran a bit."""
        return self.heard or self.t > 0.5

    def audio(self) -> bytes:
        """What was recorded, without the chirp."""
        return b"".join(self.frames[:self.chirp_at] + self.frames[self.chirp_at + self.CHIRP:])
