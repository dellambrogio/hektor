"""Mic checks in the terminal: mic-test (level vs the speech threshold) and wake-test."""
import json
import math
import time

from hektor.audio_in import RATE, Mic, WakeWord, pick_mic, rms
from hektor.config import RUNTIME, Conf


def mic_test(conf: Conf) -> None:
    """Print the mic level every 100 ms, against the background noise and the speech threshold
    Recorder would use, until Ctrl+C."""
    from collections import deque
    import queue
    chunks: queue.Queue[bytes] = queue.Queue()
    mic = Mic(conf, chunks.put)
    levels: deque[float] = deque(maxlen=50)
    print(f"mic: {pick_mic(mic.preferred)}\nbe quiet for 5 s, then talk; Ctrl+C to stop\n")
    try:
        while True:
            level = rms(chunks.get())
            levels.append(level)
            noise = sorted(levels)[len(levels) // 5]
            threshold = max(conf.num("MIN_RMS"), noise * conf.num("NOISE_FACTOR"))
            bar = "#" * min(60, round(60 * math.log10(1 + level) / math.log10(32768)))
            print(f"{level:6.0f} {'SPEECH' if level > threshold else '      '} noise {noise:5.0f} "
                  f"threshold {threshold:5.0f} {bar}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        mic.stop()


def wake_test(conf: Conf) -> None:
    """Show what the wake word spotter hears, and what a free recognizer thinks you said, until Ctrl+C.
    Saves the audio and a log in the runtime dir for a closer look."""
    import queue
    import vosk
    RUNTIME.mkdir(parents=True, exist_ok=True)
    chunks: queue.Queue[bytes] = queue.Queue()
    wake = WakeWord(conf)
    free = vosk.KaldiRecognizer(vosk.Model(str(conf.path("WAKE_MODEL"))), RATE)
    mic = Mic(conf, chunks.put)
    raw, logf = open(RUNTIME / "wake-test.raw", "wb"), open(RUNTIME / "wake-test.log", "w")

    def out(line: str) -> None:
        print(line, flush=True)
        logf.write(f"{time.strftime('%H:%M:%S')} {line}\n")
        logf.flush()

    out(f"mic: {mic.name}; say {wake.phrases[0]!r} a few times, Ctrl+C to stop")
    try:
        while True:
            data = chunks.get()
            raw.write(data)
            if wake.heard(data):
                out(f"*** WAKE (level {rms(data):.0f})")
            if free.AcceptWaveform(data):
                if text := json.loads(free.Result()).get("text"):
                    out(f"    heard: {text}")
    except KeyboardInterrupt:
        pass
    finally:
        mic.stop()
        raw.close()
        logf.close()
        print(f"saved {RUNTIME / 'wake-test.raw'} and wake-test.log")
