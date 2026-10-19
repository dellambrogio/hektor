"""Deleting what was said: the monitor app's Delete asks for it (CLEAR_REQUEST), the app does it.

Gone afterwards: the conversation in events.jsonl (what was heard and answered, wake words, misses), the
same lines in hektor.log and its rotated copies, Claude's saved conversations (~/.claude/projects, for
this account), and the last recording and spoken replies in the runtime folder. Kept: everything else in
the events and the log (starts, the camera, faces), the faces Hektor knows, the settings.
"""
import logging
import re
from pathlib import Path

from . import events
from .config import LOG, RUNTIME

log = logging.getLogger(__name__)
CLEAR_REQUEST = LOG.parent / "clear-conversation"  # the monitor app creates it, the app removes it
SAID = {"heard", "reply", "unclear", "nothing", "wake", "ignored", "listening", "interrupted", "cancelled",
        "error", "reset", "looked"}
# log lines with something said in them (voice.py, stt.py, brain.py)
SAID_LOG = re.compile(r" (?:heard \(|reply \[|saying: |whisper heard no words|claude: )")
CLAUDE = Path.home() / ".claude" / "projects" / str(Path.home()).replace("/", "-")  # claude -p runs in ~


def clear() -> None:
    kept = events.rewrite(lambda ev: ev.get("type") not in SAID)
    lines = 0
    for f in [LOG, *sorted(LOG.parent.glob(LOG.name + ".*"))]:
        lines += _filter_log(f)
    sessions = 0
    if CLAUDE.is_dir():
        for f in CLAUDE.iterdir():
            if f.is_file():
                f.unlink()
                sessions += 1
            elif f.is_dir():
                for g in sorted(f.rglob("*"), reverse=True):
                    g.rmdir() if g.is_dir() else g.unlink()
                f.rmdir()
    for pattern in ("heard.*", "reply*.wav"):
        for f in RUNTIME.glob(pattern):
            f.unlink(missing_ok=True)
    log.info("conversation deleted: %d events kept, %d log lines and %d Claude sessions removed",
             kept, lines, sessions)


def _filter_log(path: Path) -> int:
    """Remove the lines with something said in them, in place (the logger keeps appending to it)."""
    try:
        with open(path, "r+", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            keep = [line for line in lines if not SAID_LOG.search(line)]
            f.seek(0)
            f.writelines(keep)
            f.truncate()
        return len(lines) - len(keep)
    except OSError as e:
        log.error("can't clean %s: %s", path, e)
        return 0
