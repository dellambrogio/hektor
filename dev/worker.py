"""The worker (worker/ in the repo) as the monitor sees it: its two services on monitor, whether
whisper-server is busy, monitor's CPU and RAM, and the worker's log.

Busy is whisper-server using the CPU (sampled every 0.25 s): it only does that while it transcribes.
The log is the services' journal without whisper's model-loading chatter, plus each transcription's
length from the busy samples.
"""
import json
import os
import re
import subprocess
import threading
import time
from collections import deque

UNITS = ("hektor-whisper", "hektor-tunnel")
SAMPLE = 0.25          # seconds between samples
BUSY_CPU = 0.5         # whisper-server using more than this many cores: it's transcribing
TICK = os.sysconf("SC_CLK_TCK")
# the journal lines not worth showing: whisper's model loading, and what it says for every request
NOISE = ("whisper_", "system_info", "ggml_", "load_backend", "main:", "Received request", "Successfully loaded",
         "Running whisper.cpp", "Caught signal")
PROCESSING = re.compile(r"processing '[^']*' \(\d+ samples, ([\d.]+) sec\)")
ERROR = re.compile(r"error|fail|refused|timed? ?out|denied|unreachable|could not|can't", re.I)


def read_cpu() -> tuple[int, int]:
    """(busy, total) CPU time of the whole machine so far, from /proc/stat."""
    with open("/proc/stat") as f:
        user, nice, system, idle, iowait, irq, softirq, steal = map(int, f.readline().split()[1:9])
    busy = user + nice + system + irq + softirq + steal
    return busy, busy + idle + iowait


def cpu_percent(before: tuple[int, int], after: tuple[int, int]) -> float:
    return 100 * (after[0] - before[0]) / max(1, after[1] - before[1])


def read_mem() -> tuple[int, int]:
    """(used, total) RAM in kB."""
    mem = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":", 1)
            mem[key] = int(value.split()[0])
    return mem["MemTotal"] - mem["MemAvailable"], mem["MemTotal"]


def proc_time(pid: int) -> float | None:
    """CPU seconds process `pid` used so far, None if it's gone."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / TICK
    except (OSError, IndexError, ValueError):
        return None


class WorkerWatch:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.units: dict[str, dict] = {}        # name: LoadState, ActiveState, MainPID, ActiveEnterTimestamp
        self.cpu: deque[float] = deque(maxlen=int(60 / SAMPLE))  # monitor's CPU %, the last minute
        self.mem = (0, 0)
        self.busy = False
        self.last: tuple[float, float] | None = None  # the last transcription: (when it ended, seconds)
        self.log: deque[tuple[float, str, str]] = deque(maxlen=300)  # (time, kind, text): info request done error action
        self.acting = ""                        # "starting" / "stopping" while the button's command runs
        self.stopped = False
        threading.Thread(target=self._sample, name="worker-sample", daemon=True).start()
        threading.Thread(target=self._journal, name="worker-journal", daemon=True).start()

    # --- what the monitor shows ---------------------------------------------------------------

    @property
    def installed(self) -> bool:
        with self.lock:
            return bool(self.units) and all(u.get("LoadState") == "loaded" for u in self.units.values())

    def state(self, unit: str) -> str:
        with self.lock:
            return self.units.get(unit, {}).get("ActiveState", "?")

    @property
    def running(self) -> bool:
        return any(self.state(u) in ("active", "activating", "reloading") for u in UNITS)

    def since(self, unit: str) -> str:
        with self.lock:
            return self.units.get(unit, {}).get("ActiveEnterTimestamp", "")

    def snapshot(self) -> dict:
        with self.lock:
            return {"cpu": list(self.cpu), "mem": self.mem, "busy": self.busy, "last": self.last,
                    "log": list(self.log), "acting": self.acting}

    # --- the button -----------------------------------------------------------------------------

    def toggle(self) -> None:
        """Start the worker's services, or stop them if they're running."""
        if self.acting or not self.installed:
            return
        start = not self.running
        with self.lock:
            self.acting = "starting" if start else "stopping"
            self.log.append((time.time(), "action", f"{self.acting} the worker (from the monitor)"))

        def run() -> None:
            done = subprocess.run(["systemctl", "--user", "start" if start else "stop", *(f"{u}.service" for u in UNITS)],
                                  capture_output=True, text=True)
            self._units()
            with self.lock:
                self.acting = ""
                if done.returncode:
                    self.log.append((time.time(), "error", done.stderr.strip() or f"systemctl failed ({done.returncode})"))
        threading.Thread(target=run, name="worker-toggle", daemon=True).start()

    def stop(self) -> None:
        self.stopped = True

    # --- threads --------------------------------------------------------------------------------

    def _units(self) -> None:
        out = subprocess.run(["systemctl", "--user", "show", *(f"{u}.service" for u in UNITS),
                              "-p", "Id,LoadState,ActiveState,MainPID,ActiveEnterTimestamp"],
                             capture_output=True, text=True).stdout
        units = {}
        for block in out.strip().split("\n\n"):
            props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            if props.get("Id"):
                units[props["Id"].removesuffix(".service")] = props
        with self.lock:
            self.units = units

    def _sample(self) -> None:
        before, pid, used, busy_from, i = read_cpu(), 0, None, 0.0, 0
        while not self.stopped:
            time.sleep(SAMPLE)
            if i % 8 == 0:  # every 2 s
                self._units()
                with self.lock:
                    pid = int(self.units.get("hektor-whisper", {}).get("MainPID") or 0)
            i += 1
            after = read_cpu()
            pct, before = cpu_percent(before, after), after
            now_used = proc_time(pid) if pid else None
            busy = now_used is not None and used is not None and (now_used - used) / SAMPLE > BUSY_CPU
            used = now_used
            with self.lock:
                self.cpu.append(pct)
                self.mem = read_mem()
                if busy and not self.busy:
                    busy_from = time.monotonic()
                elif self.busy and not busy:
                    seconds = time.monotonic() - busy_from
                    self.last = (time.time(), seconds)
                    self.log.append((time.time(), "done", f"transcribed in {seconds:.1f} s"))
                self.busy = busy

    def _journal(self) -> None:
        while not self.stopped:
            proc = subprocess.Popen(["journalctl", "--user", "-f", "-n", "60", "-o", "json",
                                     *(arg for u in UNITS for arg in ("-u", u))],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            for line in proc.stdout:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                text = entry.get("MESSAGE")
                if not isinstance(text, str) or not text.strip() or text.startswith(NOISE):
                    continue
                t = int(entry.get("__REALTIME_TIMESTAMP", 0)) / 1e6
                unit = (entry.get("USER_UNIT") or entry.get("_SYSTEMD_USER_UNIT") or "").removesuffix(".service")
                who = unit.removeprefix("hektor-") or "worker"
                if m := PROCESSING.search(text):
                    kind, text = "request", f"request: {m.group(1)} s of speech"
                else:
                    kind = "error" if ERROR.search(text) else "info"
                with self.lock:
                    self.log.append((t, kind, f"{who}: {text}"))
            if self.stopped:
                return
            time.sleep(3)
