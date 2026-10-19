"""Speaking: piper (espeak-ng for other languages) → ffmpeg voice effect → pw-play.

A reply is spoken a sentence at a time: the next one is made while the one before it plays.
While it plays, `level` follows the loudness of the speech, for the eyes.
"""
import array
import logging
import math
import re
import subprocess
import threading
import time
import wave
from pathlib import Path

from .audio_in import EC_SPEAKER, pw_node
from .config import DATA, RUNTIME, Conf

log = logging.getLogger(__name__)
WINDOW = 0.02  # seconds per step of the loudness envelope
# evens out loud and soft syllables so the voice carries on small speakers; then it's brought to full level
COMPRESS = "acompressor=threshold=-26dB:ratio=4:attack=4:release=80:makeup=2"
_ec = (0.0, False)  # (when checked, whether the echo canceller's speaker exists)
# the end of a sentence: . ! ? … (and closing quotes or brackets) before a space, or a line break
SENTENCE_END = re.compile(r"[.!?…][\"')\]]*(?=\s)|\n")


LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")       # [words](https://...): just the words
URL = re.compile(r"\(?\b(?:https?://|www\.)\S+")
MARKUP = re.compile(r"[*_#`>]+")


def speakable(text: str) -> str:
    """What of `text` can be said aloud: no links or addresses, no markdown; "" if nothing's left."""
    text = MARKUP.sub("", URL.sub("", LINK.sub(r"\1", text)))
    text = " ".join(text.split()).strip(" -–—:;,")
    return text if any(c.isalnum() for c in text) else ""


def sentences(text: str) -> tuple[list[str], str]:
    """The whole sentences at the start of `text`, and the rest (maybe a sentence still coming)."""
    done, start = [], 0
    for end in SENTENCE_END.finditer(text):
        if end.end() == len(text):  # nothing after it yet: "3." may be "3.5"
            break
        if sentence := text[start:end.end()].strip():
            done.append(sentence)
        start = end.end()
    return done, text[start:]


def pw_play(wav: Path) -> subprocess.Popen:
    """Start playing `wav`, through the echo canceller when there is one so the mic doesn't hear it."""
    global _ec
    if time.monotonic() - _ec[0] > 10:  # pw-dump is slow-ish, sounds should be snappy
        _ec = (time.monotonic(), pw_node(EC_SPEAKER) is not None)
    target = ["--target", EC_SPEAKER] if _ec[1] else []
    return subprocess.Popen(["pw-play", *target, str(wav)], stderr=subprocess.DEVNULL)


class Speaker:
    def __init__(self, conf: Conf) -> None:
        self.conf = conf
        self._voice = None
        self.proc: subprocess.Popen | None = None
        self.envelope: list[float] = []
        self.started = 0.0
        self._loading = threading.Lock()

    def _piper(self):
        with self._loading:
            if self._voice is None:
                from piper import PiperVoice
                t = time.monotonic()
                self._voice = PiperVoice.load(DATA / "voices" / f"{self.conf['VOICE']}.onnx")
                log.info("loaded voice %s in %.1f s", self.conf["VOICE"], time.monotonic() - t)
        return self._voice

    def warm_up(self) -> None:
        """Load the voice and make a word with it, effect included, in the background, so the first
        reply doesn't wait for it (seconds on hektor)."""
        def run() -> None:
            t = time.monotonic()
            if self.synth("Hello.", name="warmup"):
                log.info("voice warmed up in %.1f s", time.monotonic() - t)
        threading.Thread(target=run, name="voice-warm-up", daemon=True).start()

    def effect(self, rate: int) -> str:
        c = self.conf
        match c["EFFECT"]:
            case "small":
                p = c.num("SMALL_PITCH")
                return (f"asetrate={rate}*{p},aresample={rate},atempo={1 / p:.4f},"
                        f"acrusher=bits={c['SMALL_BITS']}:mode=log:mix=0.5:aa=1,"
                        f"aecho=0.8:0.4:{c['SMALL_ECHO_MS']}:0.3,highpass=f={c['SMALL_HIGHPASS']}")
            case "kitt":
                return (f"asetrate={rate}*0.94,aresample={rate},atempo={1 / 0.94:.4f},"
                        "aecho=0.8:0.45:12|24:0.3|0.15,highpass=f=140,lowpass=f=6500")
            case "robot":
                return ("afftfilt=real='hypot(re,im)*sin(0)':imag='hypot(re,im)*cos(0)'"
                        ":win_size=512:overlap=0.75,volume=1.5")
            case "custom":
                return c["FX_CUSTOM"]
        return ""

    def synth(self, text: str, lang: str = "en", name: str = "reply", pause: bool = False) -> Path | None:
        """Turn `text` into RUNTIME/`name`.wav, ready to play; `pause`: start with SENTENCE_PAUSE of
        silence (it follows another sentence)."""
        RUNTIME.mkdir(parents=True, exist_ok=True)
        out, raw = RUNTIME / f"{name}.wav", RUNTIME / f"{name}-raw.wav"
        c = self.conf
        if lang == "en" and (DATA / "voices" / f"{c['VOICE']}.onnx").exists():
            from piper import SynthesisConfig
            voice = self._piper()
            syn = SynthesisConfig(length_scale=c.num("SPEED"),
                                  noise_scale=c.opt("EXPRESSIVENESS"), noise_w_scale=c.opt("RHYTHM_VARIATION"))
            rate = voice.config.sample_rate
            silence = b"\0\0" * int(rate * c.num("SENTENCE_PAUSE"))
            with wave.open(str(raw), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(rate)
                for i, chunk in enumerate(voice.synthesize(text, syn)):  # one chunk per sentence
                    w.writeframes((silence if i or pause else b"") + chunk.audio_int16_bytes)
            fx = self.effect(rate)
        else:
            log.info("speaking %s with espeak-ng", lang)
            if subprocess.run(["espeak-ng", "-v", lang, "-s", c["ESPEAK_SPEED"], "-p", c["ESPEAK_PITCH"],
                               "-w", str(raw), text]).returncode:
                return None
            fx = ""
        chain = f"{fx},{COMPRESS}" if fx else COMPRESS
        if subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(raw), "-af", chain, str(out)]).returncode:
            log.error("voice effect failed, speaking without it")
            raw.replace(out)
        _normalize(out, c.num("VOLUME"))
        return out

    def start(self, wav: Path) -> None:
        """Start playing `wav` (after what's playing, if anything); wait() for it."""
        envelope = _envelope(wav)
        self.wait()
        self.envelope = envelope
        self.proc = pw_play(wav)
        self.started = time.monotonic()

    def wait(self) -> None:
        """Until what's playing is done or stop() was called."""
        if proc := self.proc:
            proc.wait()
            self.proc = None

    def play(self, wav: Path) -> None:
        """Play `wav`, returning when it's done or stop() was called."""
        self.start(wav)
        self.wait()

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()

    def say(self, text: str, lang: str = "en") -> None:
        if wav := self.synth(text, lang):
            self.play(wav)

    @property
    def level(self) -> float:
        """Loudness of what's playing now, 0-1."""
        if not self.proc:
            return 0.0
        i = int((time.monotonic() - self.started) / WINDOW)
        return self.envelope[i] if 0 <= i < len(self.envelope) else 0.0


def _normalize(wav: Path, volume: float) -> None:
    """Scale `wav` so its loudest peak is at `volume` (0-1) of full scale."""
    with wave.open(str(wav), "rb") as w:
        params, frames = w.getparams(), w.readframes(w.getnframes())
    if params.sampwidth != 2:
        return
    samples = array.array("h", frames)
    peak = max(map(abs, samples), default=0)
    if not peak:
        return
    gain = min(volume, 1.0) * 0.97 * 32767 / peak
    samples = array.array("h", (max(-32768, min(32767, round(s * gain))) for s in samples))
    with wave.open(str(wav), "wb") as w:
        w.setparams(params)
        w.writeframes(samples.tobytes())


def _envelope(wav: Path) -> list[float]:
    """Loudness every WINDOW seconds, scaled so that loud speech is about 1."""
    with wave.open(str(wav), "rb") as w:
        if w.getsampwidth() != 2:
            return []
        rate, channels = w.getframerate(), w.getnchannels()
        samples = array.array("h", w.readframes(w.getnframes()))[::channels]
    step = max(1, int(rate * WINDOW))
    levels = [math.sqrt(sum(s * s for s in samples[i:i + step]) / step) for i in range(0, len(samples), step)]
    loud = sorted(levels)[int(len(levels) * 0.95)] if levels else 0
    return [min(1.0, x / loud) for x in levels] if loud else levels
