"""Speech to text with whisper.cpp.

`whisper-server` runs next to the app with the model loaded, so a question doesn't wait for the model
to load (seconds on hektor, more right after a boot). When it isn't there or isn't up yet,
`whisper-cli` does it instead. A faster computer (monitor) can do it for hektor (the worker, WHISPER_WORKER).
"""
import json
import logging
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from collections.abc import Callable
from pathlib import Path

from .audio_in import RATE
from .config import RUNTIME, Conf

log = logging.getLogger(__name__)

# whisper tags noise as e.g. [BLANK_AUDIO], [Motor], (wind blowing), alone or around the words
NOISE = re.compile(r"[\[(][^\])]*[\])]")


def write_wav(path: Path, audio: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(audio)


class Server:
    """whisper-server on a free local port, started in the background; `url` is set once it answers."""

    def __init__(self, conf: Conf) -> None:
        self.conf = conf
        self.url: str | None = None
        self.proc: subprocess.Popen | None = None
        self.stopped = False
        if not shutil.which("whisper-server"):
            log.info("no whisper-server: using whisper-cli for each question")
            return
        threading.Thread(target=self._start, name="whisper-server", daemon=True).start()

    def _start(self) -> None:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        t = time.monotonic()
        self.proc = subprocess.Popen(
            ["whisper-server", "-m", str(self.conf.path("WHISPER_MODEL")), "-l", self.conf["WHISPER_LANGUAGE"],
             "-t", self.conf["WHISPER_THREADS"], "-nt", "--host", "127.0.0.1", "--port", str(port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}/inference"
        while not self.stopped and self.proc.poll() is None:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1).close()
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.2)
        if self.stopped or self.proc.poll() is not None:
            log.warning("whisper-server didn't start: using whisper-cli")
            return
        # the first transcription is slower (buffers, caches): do it on a second of silence now
        RUNTIME.mkdir(parents=True, exist_ok=True)
        wav = RUNTIME / "warmup.wav"
        write_wav(wav, b"\0\0" * RATE)
        try:
            _post(url, wav, 60)
        except (OSError, ValueError) as e:
            log.warning("whisper-server warm-up failed: %s", e)
        self.url = url
        log.info("whisper-server ready in %.1f s", time.monotonic() - t)

    def transcribe(self, wav: Path, ctx: int) -> dict | None:
        """whisper's result for `wav`, None if the server failed (then whisper-cli takes over)."""
        if not self.url:
            return None
        try:
            return _post(self.url, wav, 120, ctx)
        except (OSError, ValueError) as e:
            log.error("whisper-server failed (%s): using whisper-cli", e)
            if self.proc and self.proc.poll() is not None:
                self.url = None
            return None

    def stop(self) -> None:
        self.stopped = True
        if self.proc:
            self.proc.terminate()


class Worker:
    """whisper-server on another computer (worker/ in the repo), at WHISPER_WORKER: tried first, it's
    faster. When it isn't there (the tunnel's down: refused at once) it's whisper here; when it fails or
    is slow, whisper here too, and the worker is left alone for a minute."""

    def __init__(self, conf: Conf) -> None:
        self.url = conf.get("WHISPER_WORKER") or None
        self.skip_until = 0.0
        self.up: bool | None = None

    def transcribe(self, wav: Path, seconds: float, ctx: int) -> dict | None:
        if not self.url or time.monotonic() < self.skip_until:
            return None
        try:
            result = _post(self.url, wav, 2 + seconds * 0.3, ctx)
        except urllib.error.URLError as e:
            if isinstance(e.reason, ConnectionRefusedError):
                self._is(False, "not there")
                return None
            return self._failed(e.reason)
        except (OSError, ValueError) as e:
            return self._failed(e)
        self._is(True, "there")
        return result

    def _failed(self, why) -> None:
        log.warning("worker failed (%s): whisper here, worker left alone for a minute", why)
        self.skip_until = time.monotonic() + 60
        self.up = None

    def _is(self, up: bool, what: str) -> None:
        if up != self.up:
            log.info("worker %s (%s)", what, self.url)
        self.up = up


def _post(url: str, wav: Path, timeout: float, ctx: int = 0) -> dict:
    boundary = uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
             for name, value in (("response_format", "verbose_json"), ("temperature", "0"),
                                 ("token_timestamps", "false"), ("no_language_probabilities", "true"),
                                 ("audio_ctx", str(ctx)))]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{wav.name}"\r\n'
                 "Content-Type: audio/wav\r\n\r\n".encode() + wav.read_bytes() + b"\r\n")
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(url, body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def audio_ctx(conf: Conf, seconds: float) -> int:
    """How much of whisper's 30 s window to encode (0: all of it). Encoding is most of whisper's time and
    always covers 30 s; just the recording, with some to spare, is ~3x faster for a 5 s question."""
    if not conf.yes("WHISPER_SHORT_CONTEXT"):
        return 0
    return min(1500, int(seconds * 50) + 128)  # the encoder has 50 frames a second, 1500 in all


def transcribe(conf: Conf, wav: Path, run: Callable[[list[str]], str | None],
               server: Server | None = None, worker: Worker | None = None) -> tuple[str, str, str] | None:
    """(text, language, where whisper ran) of what was said in `wav`, None when nothing was or it was
    cancelled. The worker if it's there, else whisper-server here, else whisper-cli."""
    with wave.open(str(wav)) as w:
        seconds = w.getnframes() / w.getframerate()
    ctx = audio_ctx(conf, seconds)
    # whisper-server names the language ("english"): the code is the one asked for (auto: assume English)
    lang = conf["WHISPER_LANGUAGE"] if conf["WHISPER_LANGUAGE"] != "auto" else "en"
    if worker and (result := worker.transcribe(wav, seconds, ctx)) is not None:
        raw, where = result.get("text", ""), "worker"
    elif server and (result := server.transcribe(wav, ctx)) is not None:
        raw, where = result.get("text", ""), "server"
    else:
        base = wav.with_suffix("")
        out = run(["whisper-cli", "-m", str(conf.path("WHISPER_MODEL")), "-l", conf["WHISPER_LANGUAGE"],
                   "-t", conf["WHISPER_THREADS"], "-ac", str(ctx), "-nt", "-np", "-oj", "-of", str(base), "-f", str(wav)])
        if out is None:
            return None
        result = json.loads(base.with_suffix(".json").read_text())
        raw, where = " ".join(t["text"] for t in result.get("transcription", [])), "cli"
        lang = result.get("result", {}).get("language", lang)
    raw = raw.strip()
    text = " ".join(NOISE.sub("", raw).split())
    if not text:
        log.info("whisper heard no words%s", f": {raw}" if raw else "")
        return None
    return text, lang, where
