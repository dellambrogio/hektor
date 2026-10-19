"""Settings from hektor.conf, then ~/.config/hektor/hektor.conf.

The files are sourced by bash, so they can use quotes, ~, $VARS and line continuations.
"""
import os
import subprocess
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
FILES = [APP / "hektor.conf", Path.home() / ".config/hektor/hektor.conf"]
DATA = Path.home() / ".local/share/hektor"
RUNTIME = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "hektor"
LOG = Path.home() / ".local/state/hektor/hektor.log"


class Conf(dict):
    def num(self, key: str) -> float:
        return float(self[key])

    def int(self, key: str) -> int:
        return int(self[key])

    def opt(self, key: str) -> float | None:
        """A number that may be left empty."""
        return float(self[key]) if self.get(key) else None

    def yes(self, key: str) -> bool:
        return self.get(key, "").lower() in ("yes", "true", "on", "1")

    def path(self, key: str) -> Path:
        return Path(os.path.expanduser(self[key]))


def stamp() -> tuple[float, ...]:
    """When the settings files last changed, to notice a change."""
    return tuple(f.stat().st_mtime if f.exists() else 0.0 for f in FILES)


def load() -> Conf:
    script = 'set -a; for f; do if [[ -f "$f" ]]; then source "$f"; fi; done; env -0'
    out = subprocess.run(["bash", "-c", script, "_", *map(str, FILES)], capture_output=True,
                         env={"HOME": str(Path.home())}, check=True).stdout
    return Conf(kv.split("=", 1) for kv in out.decode().split("\0") if "=" in kv)
