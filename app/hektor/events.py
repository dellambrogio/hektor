"""What Hektor does, one JSON line per event, for the monitor (dev/monitor.py) to follow.

~/.local/state/hektor/events.jsonl: {"t": unix time, "type": ..., ...fields}. Types: start, stop, state,
mood, model (Claude's), face, camera (on or off), recognition (on or off), person (who the face is), looked (someone
known looked at it: it listens), enrolled, forgot,
wake, ignored (a wake word without a voice), listening, nothing, heard, unclear, reply, interrupted,
cancelled, reset, error, cleared (the conversation was deleted, history.py).
"""
import json
import threading
import time

from .config import LOG

FILE = LOG.parent / "events.jsonl"
MAX = 2_000_000  # bytes; at start, a bigger file is cut down to its newer half
_lock = threading.Lock()


def start() -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    if FILE.exists() and FILE.stat().st_size > MAX:
        data = FILE.read_bytes()[-MAX // 2:]
        FILE.write_bytes(data[data.find(b"\n") + 1:])
    emit("start")


def rewrite(keep) -> int:
    """Keep only the events `keep(event)` says yes to (history.py deletes the conversation); how many."""
    with _lock:
        try:
            lines = FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        kept = []
        for line in lines:
            try:
                if keep(json.loads(line)):
                    kept.append(line)
            except ValueError:
                pass
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text("".join(line + "\n" for line in kept), encoding="utf-8")
        tmp.replace(FILE)  # emit() opens the file for each line, so it writes to the new one
        return len(kept)


def emit(type_: str, **fields) -> None:
    line = json.dumps({"t": round(time.time(), 3), "type": type_, **fields}, ensure_ascii=False)
    with _lock:
        try:
            with open(FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # the robot keeps going without its event log
