"""Pictures for the monitor (dev/monitor.py): the camera and the screen, only while it's watching.

The monitor keeps touching RUNTIME/<name>.watch; while that's recent, the app writes RUNTIME/<name>.jpg
for it to fetch, and removes it once nobody watches, so no picture lies around otherwise.
"""
import logging
import time

from .config import RUNTIME

log = logging.getLogger(__name__)
WATCH_FOR = 5  # seconds a touch of the watch file keeps the pictures coming


def watched(name: str) -> bool:
    try:
        return time.time() - (RUNTIME / f"{name}.watch").stat().st_mtime < WATCH_FOR
    except OSError:
        return False


def write(name: str, jpg: bytes) -> None:
    """Write the picture whole or not at all (the monitor may be reading the last one)."""
    path = RUNTIME / f"{name}.jpg"
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(jpg)
        tmp.replace(path)
    except OSError as e:  # the robot keeps working without it
        log.debug("can't share the %s picture: %s", name, e)


def remove(name: str) -> None:
    (RUNTIME / f"{name}.jpg").unlink(missing_ok=True)
