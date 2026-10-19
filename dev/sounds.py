"""sound: play Hektor's noises from the terminal."""
import time

from hektor.config import Conf
from hektor.sounds import SOUNDS, Sounds


def play_once(conf: Conf, name: str) -> None:
    """Play one sound (or all, for "all") and wait: for trying them out."""
    s = Sounds(conf)
    for n in (list(SOUNDS) if name == "all" else [name]):
        if n not in SOUNDS:
            raise SystemExit(f"no sound {n!r}; there are: all, {', '.join(SOUNDS)}")
        print(n)
        s.play(n, wait=True)
        time.sleep(0.4)
