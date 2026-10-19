"""Monitor: watch Hektor running on hektor from here (monitor), over SSH, and the worker here.

Top: whether the service is running (since when), the connection, what Hektor is doing now, its mood and
how long whisper and Claude took. Then what its screen shows (the eyes) and what its camera sees (the
faces boxed), a few times a second, with a button for the camera; and the faces: who it sees, a button
for face detection (recognition), and who it knows (names only, from faces.json).
Below: the conversation, what it heard and what it answered, with the wake words, interruptions and
misses in between (Delete, clicked twice, deletes it here and on hektor: hektor/history.py); the worker (worker/ in the repo,
dev/worker.py): running or not with a button to start and stop it, busy while it transcribes, its log;
and the system: CPU (the last minute) and RAM of hektor and monitor.

It follows ~/.local/state/hektor/events.jsonl there (written by hektor/events.py) and asks systemd
about hektor.service every 2 s. For the pictures it keeps touching screen.watch and camera.watch there,
which makes the app write screen.jpg and camera.jpg (hektor/share.py), and sends each new one. "local"
as the host watches this computer instead.

Drag the gaps between the panels to resize them (the layout and window size are kept in
~/.config/hektor/monitor.json). The camera panel's buttons turn Hektor's camera and face recognition on
and off (CAMERA, FACE_RECOGNITION in hektor's ~/.config/hektor/hektor.conf, which the app applies while
running). Keys: c show / hide the camera picture here, w start / stop the worker, r reset the layout,
mouse wheel / PageUp / PageDown scroll the conversation, End back to the latest, q / Esc quit.
"""
import base64
import io
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import pygame

from hektor.config import FILES
from hektor.eyes import COLOR
from .console import BG, DIM, LINE, PANEL, SPEECH, TEXT, WARN
from .worker import UNITS, WorkerWatch, cpu_percent

log = logging.getLogger(__name__)

DEPLOY_CONF = Path(__file__).resolve().parents[1] / "deploy.conf"  # the repo's, see deploy.conf.example
SIZE = (1180, 760)
LAYOUT = FILES[-1].parent / "monitor.json"  # ~/.config/hektor/monitor.json
TOP_H = 300            # default height of the screen, camera and faces panels
CUTS = (0.38, 0.72)    # default dividers between them, as fractions of the width
WORKER_CUT = 0.6       # default divider between the conversation and the worker
GAP, MIN_W, MIN_TOP, MIN_TALK, MIN_WORKER = 8, 150, 120, 120, 300
GOOD, MOOD, BUSY = (90, 200, 120), (200, 150, 240), (240, 190, 60)
DIVIDERS = ("v1", "v2", "v3", "hz")
SETTINGS = {"camera": "CAMERA", "recognition": "FACE_RECOGNITION"}  # the camera and faces buttons
MODELS = [("claude-haiku-5-5", "Haiku 5.5", "fastest"), ("claude-sonnet-5-5", "Sonnet 5.5", ""),
          ("claude-opus-5-5", "Opus 5.5", "smartest")]  # the conversation panel's menu: MODEL on hektor
SYSTEM_H = 130         # height of the system panel, under the worker
LOG_COLORS = {"request": TEXT, "done": BUSY, "error": WARN, "action": COLOR, "info": DIM}
STATES = {"idle": COLOR, "sleeping": DIM, "listening": SPEECH, "thinking": MOOD, "speaking": COLOR,
          "off": WARN, "?": DIM}

# runs there: the service status every 2 s; the machine's CPU time and RAM every second; who it knows by
# face when that changes; the pictures as they come (the camera if CAMERA=1); and the events
REMOTE = r'''
f=~/.local/state/hektor/events.jsonl
r=${XDG_RUNTIME_DIR:-/tmp}/hektor
mkdir -p "${f%/*}"; touch "$f"
pics=screen
[[ $CAMERA == 1 ]] && pics="screen camera"
fj=~/.local/share/hektor/faces.json
i=0
declare -A last
while :; do
    for p in $pics; do
        [[ -d $r ]] || break
        touch "$r/$p.watch"
        m=$(stat -c %y "$r/$p.jpg" 2>/dev/null)
        if [[ -n $m && $m != "${last[$p]}" ]]; then
            last[$p]=$m
            printf '{"type":"frame","name":"%s","jpg":"%s"}\n' "$p" "$(base64 -w0 "$r/$p.jpg" 2>/dev/null)"
        fi
    done
    if (( i % 4 == 0 )); then
        read -r _ u n s idle io irq sirq st _ < /proc/stat
        read -r mt ma < <(awk '/^MemTotal:/ {t=$2} /^MemAvailable:/ {a=$2} END {print t, a}' /proc/meminfo)
        printf '{"type":"system","busy":%d,"total":%d,"mem_total":%d,"mem_avail":%d}\n' \
            $((u + n + s + irq + sirq + st)) $((u + n + s + idle + io + irq + sirq + st)) "$mt" "$ma"
    fi
    if (( i % 8 == 0 )); then
        fm=$(stat -c %y "$fj" 2>/dev/null || echo 0)
        if [[ $fm != "$faces_m" ]]; then  # who Hektor knows by face: names and how many fingerprints only
            faces_m=$fm
            printf '{"type":"faces","people":%s}\n' "$(python3 -c 'import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    d = {}
print(json.dumps({n: len(v) for n, v in d.items()}))' "$fj")"
        fi
        printf '{"type":"service","active":"%s","since":"%s"}\n' \
            "$(systemctl --user is-active hektor 2>/dev/null)" \
            "$(systemctl --user show -p ActiveEnterTimestamp --value hektor 2>/dev/null)"
    fi
    i=$((i + 1))
    sleep 0.25
done &
trap 'kill $! 2>/dev/null' EXIT
tail -n 1000 -F "$f" 2>/dev/null
'''


class Feed:
    """Reads the events and the service status from `host` on a thread, reconnecting when it drops."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.events: list[dict] = []
        self.service: dict = {}
        self.cpu: deque[float] = deque(maxlen=60)  # the machine's CPU %, every second: the last minute
        self.mem = (0, 0)                          # its RAM (used, total) in kB
        self.people: dict[str, int] | None = None  # who Hektor knows by face: name, fingerprints
        self._cpu_time: tuple[int, int] | None = None  # (busy, total) CPU time the last time
        self.connected = False
        self.error = ""
        self.camera = True
        self.frames: dict[str, tuple[float, bytes]] = {}  # screen / camera: (when it came, JPEG)
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.stopped = self.restarting = False
        threading.Thread(target=self._run, name="feed", daemon=True).start()

    def _cmd(self) -> list[str]:
        script = f"CAMERA={int(self.camera)}\n{REMOTE}"
        if self.host == "local":
            return ["bash", "-c", script]
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=5",
                "-o", "ServerAliveCountMax=2", self.host, "bash -c " + shlex.quote(script)]

    def _run(self) -> None:
        while not self.stopped:
            self.proc = subprocess.Popen(self._cmd(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            fresh = True
            for line in self.proc.stdout:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                with self.lock:
                    self.connected, self.error = True, ""
                    if ev.get("type") == "service":
                        self.service = ev
                        continue
                    if ev.get("type") == "faces":
                        self.people = ev.get("people", {})
                        continue
                    if ev.get("type") == "cleared":  # the conversation was deleted there: read the file again
                        self.restarting = True
                        self.proc.terminate()
                        continue
                    if ev.get("type") == "system":
                        cpu_time = (ev["busy"], ev["total"])
                        if self._cpu_time:
                            self.cpu.append(cpu_percent(self._cpu_time, cpu_time))
                        self._cpu_time = cpu_time
                        self.mem = (ev["mem_total"] - ev["mem_avail"], ev["mem_total"])
                        continue
                    if ev.get("type") == "frame":
                        try:
                            self.frames[ev.get("name", "camera")] = (time.monotonic(), base64.b64decode(ev["jpg"]))
                        except ValueError:
                            pass
                        continue
                    if fresh:  # the reconnect replays the file's end: start over from it
                        self.events, fresh = [], False
                    self.events.append(ev)
            if self.stopped:
                return
            if self.restarting:
                self.restarting = False
                continue
            err = self.proc.stderr.read().strip().splitlines()
            with self.lock:
                self.connected = False
                self.error = err[-1] if err else "connection closed"
            time.sleep(3)

    def stop(self) -> None:
        self.stopped = True
        if self.proc:
            self.proc.terminate()

    def power_off(self) -> None:
        """Ask the app there to switch the machine off: it runs `systemctl poweroff` from the desktop
        session it's logged into, which needs no sudo (over SSH it would ask for a password)."""
        script = "mkdir -p ~/.local/state/hektor && touch ~/.local/state/hektor/power-off"
        cmd = ["bash", "-c", script] if self.host == "local" else [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", self.host, script]

        def run() -> None:
            done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            if done.returncode:
                log.error("couldn't ask to power off: %s", done.stderr.strip())
        threading.Thread(target=run, name="power-off", daemon=True).start()

    def clear_conversation(self) -> None:
        """Ask the app there to delete the conversation (history.py): it does within a second."""
        script = "mkdir -p ~/.local/state/hektor && touch ~/.local/state/hektor/clear-conversation"
        cmd = ["bash", "-c", script] if self.host == "local" else [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", self.host, script]

        def run() -> None:
            done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            if done.returncode:
                log.error("couldn't ask to delete the conversation: %s", done.stderr.strip())
        threading.Thread(target=run, name="clear", daemon=True).start()

    def faces_command(self, args: list[str], done) -> None:
        """Run `dev/run.sh faces ARGS` there (forget, rename), in the background; then done(error or "")."""
        if self.host == "local":
            cmd = [str(Path(__file__).resolve().parent / "run.sh"), "faces", *args]
        else:
            cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", self.host,
                   "cd ~/hektor && dev/run.sh faces " + " ".join(shlex.quote(a) for a in args)]

        def run() -> None:
            out = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            error = "" if out.returncode == 0 else (out.stderr.strip().splitlines() or ["failed"])[-1]
            if error:
                log.error("faces %s: %s", " ".join(args), error)
            done(error)
        threading.Thread(target=run, name="faces", daemon=True).start()

    def set_setting(self, key: str, value: bool | str) -> None:
        """Set CAMERA, FACE_RECOGNITION (on or off) or MODEL in that machine's own hektor.conf, which the
        app notices and applies while it runs (and keeps after a restart)."""
        value = ("yes" if value else "no") if isinstance(value, bool) else value
        script = rf'''f=~/.config/hektor/hektor.conf; mkdir -p "${{f%/*}}"; touch "$f"
line="{key}={value}              # from the monitor app, $(date '+%F %H:%M')"
if grep -q '^{key}=' "$f"; then sed -i "s|^{key}=.*|$line|" "$f"; else echo "$line" >> "$f"; fi'''
        cmd = ["bash", "-c", script] if self.host == "local" else [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", self.host, script]

        def run() -> None:
            done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True)
            if done.returncode:
                log.error("couldn't set %s=%s: %s", key, value, done.stderr.strip())
        threading.Thread(target=run, name="set-setting", daemon=True).start()

    def toggle_camera(self) -> None:
        """Show the camera picture here or not: reconnect with the other setting."""
        with self.lock:
            self.camera = not self.camera
            self.frames.pop("camera", None)
        self.restarting = True
        if self.proc:
            self.proc.terminate()

    def snapshot(self) -> tuple[list[dict], dict, bool, str]:
        with self.lock:
            return list(self.events), dict(self.service), self.connected, self.error

    def system(self) -> tuple[list[float], tuple[int, int]]:
        """The machine's CPU % over the last minute, and its RAM (used, total) in kB."""
        with self.lock:
            return list(self.cpu), self.mem

    def known(self) -> dict[str, int] | None:
        """Who Hektor knows by face (name: fingerprints), None until it's been read."""
        with self.lock:
            return None if self.people is None else dict(self.people)

    def picture(self, name: str = "camera") -> tuple[float, bytes] | None:
        with self.lock:
            return self.frames.get(name)


def now_state(events: list[dict]) -> dict:
    """What Hektor is doing, from the events since it last started."""
    st = {"state": "?", "face": None, "mood": None, "stt": None, "stt_on": None, "claude": None, "wait": None,
          "started": None, "camera": None, "recognition": None, "person": None, "model": None}
    for ev in events:
        match ev["type"]:
            case "start":
                st.update(state="idle", face=None, mood=None, started=ev["t"], camera=None, recognition=None,
                          person=None, model=None)
            case "camera":
                st["camera"] = ev.get("on")
            case "recognition":
                st["recognition"] = ev.get("on")
            case "model":
                st["model"] = ev.get("name")
            case "stop":
                st["state"] = "off"
            case "state":
                st["state"] = ev["state"]
            case "face":
                st["face"] = ev["seen"]
                if not ev["seen"]:
                    st["person"] = None
            case "person":
                st["person"] = ev.get("name")
            case "mood":
                st["mood"] = ev["mood"]
            case "heard":
                st["stt"], st["stt_on"] = ev.get("stt"), ev.get("stt_on")
            case "unclear":
                st["stt"] = ev.get("stt")
            case "reply":
                st["claude"], st["mood"], st["wait"] = ev.get("claude"), ev.get("mood"), ev.get("wait")
    return st


def transcript(events: list[dict]) -> list[tuple[str, float, str, str]]:
    """The conversation as (kind, time, label, text): kind is you, hektor, info, error or rule."""
    rows: list[tuple[str, float, str, str]] = []
    for ev in events:
        t, kind = ev["t"], ev["type"]
        match kind:
            case "start":
                rows.append(("rule", t, "", "Hektor started"))
            case "stop":
                rows.append(("rule", t, "", "Hektor stopped"))
            case "shutdown":
                rows.append(("rule", t, "", "hektor shutting down"))
            case "reset":
                rows.append(("rule", t, "", "new conversation"))
            case "wake":
                rows.append(("info", t, "", f"wake word (level {ev.get('level')})"))
            case "ignored":
                rows.append(("info", t, "", f"wake word ignored: no voice (level {ev.get('level')})"))
            case "listening" if ev.get("why") not in ("wake word", "follow-up", "question", "seen"):
                rows.append(("info", t, "", f"listening ({ev.get('why')})"))
            case "nothing" if ev.get("why") == "follow-up":
                rows.append(("rule", t, "", "conversation over"))
            case "nothing" if ev.get("why") == "seen":
                pass  # they looked at it and didn't talk
            case "looked":
                rows.append(("info", t, "", f"{ev.get('name')} looked at it: listening"))
            case "nothing":
                rows.append(("info", t, "", f"nothing said ({ev.get('seconds')} s)"))
            case "unclear":
                rows.append(("info", t, "", f"didn't catch that (whisper {ev.get('stt')} s)"))
            case "interrupted":
                rows.append(("info", t, "", f"interrupted while {ev.get('state')} ({ev.get('why')})"))
            case "cancelled":
                rows.append(("info", t, "", f"cancelled ({ev.get('what')})"))
            case "error":
                rows.append(("error", t, "", ev.get("text", "")))
            case "person" if ev.get("name"):
                rows.append(("info", t, "", f"that's {ev['name']}"))
            case "enrolled":
                rows.append(("info", t, "", f"remembered {ev.get('name')}'s face ({ev.get('samples')} fingerprints)"))
            case "forgot":
                rows.append(("info", t, "", f"forgot {ev.get('name')}'s face"))
            case "heard":
                where = f" ({ev['stt_on']})" if ev.get("stt_on") else ""
                rows.append(("you", t, f"you · {ev.get('lang')} · whisper {ev.get('stt')} s{where}", ev.get("text", "")))
            case "reply":
                if ev.get("new"):  # this question started a new conversation: mark it before the question
                    i = next((i for i in range(len(rows) - 1, -1, -1) if rows[i][0] == "you"), len(rows))
                    rows.insert(i, ("rule", t, "", "new conversation"))
                rows.append(("hektor", t, f"Hektor · {ev.get('mood')} · claude {ev.get('claude')} s"
                             + (f" · talking after {ev['wait']} s" if ev.get("wait") is not None else ""),
                             ev.get("text", "")))
    return rows


def wrap(text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        test = f"{line} {word}".strip()
        if font.size(test)[0] <= width or not line:
            line = test
        else:
            lines.append(line)
            line = word
    return lines + ([line] if line else [])


class Monitor:
    def __init__(self, host: str) -> None:
        self.host = host
        saved = self.load_layout()
        self.top_h, self.cuts = saved.get("top_h", TOP_H), list(saved.get("top_cuts", CUTS))
        self.wcut = saved.get("worker_cut", WORKER_CUT)
        self.cleared = saved.get("cleared", 0.0)  # the conversation before this (unix time) is deleted here
        self.screen = pygame.display.set_mode(tuple(saved.get("size", SIZE)), pygame.RESIZABLE)
        self.drag: str | None = None  # the divider being dragged: v1, v2, v3 or hz
        pygame.display.set_caption(f"Hektor monitor · {host}")
        self.font = pygame.font.Font(None, 24)
        self.small = pygame.font.Font(None, 19)
        self.big = pygame.font.Font(None, 54)
        self.title = pygame.font.Font(None, 32)
        self.feed = Feed(host)
        self.worker = WorkerWatch()
        self.buttons: dict[str, pygame.Rect] = {}  # the buttons where they were last drawn: worker, camera
        self.wanted: dict[str, tuple[bool, float]] = {}  # setting: (on, until when) after its button
        self.confirm: tuple[str, float] | None = None  # (name, until when): the trash can asks "delete?"
        self.menu_open = False             # the model menu
        self.powering_off = 0.0            # when Shut down was confirmed
        self.model_wanted: tuple[str, float] | None = None  # (model, until when) after picking one
        self.editing: dict | None = None    # renaming someone: {"old": name, "text": what's typed}
        self.face_busy: dict[str, str] = {}  # name: "deleting…" / "renaming…" while it's done there
        self.face_error: tuple[str, float] | None = None  # (what went wrong, until when it's shown)
        self.scroll = 0  # pixels up from the latest
        self.pictures: dict[str, tuple[float, pygame.Surface]] = {}  # name: (when it came, picture)
        self.running = True

    def run(self) -> None:
        clock = pygame.time.Clock()
        while self.running:
            clock.tick(15)
            for ev in pygame.event.get():
                self.event(ev)
            self.draw()
            pygame.display.flip()
        self.feed.stop()
        self.worker.stop()
        self.save_layout()

    @staticmethod
    def load_layout() -> dict:
        try:
            return json.loads(LAYOUT.read_text())
        except (OSError, ValueError):
            return {}

    def save_layout(self) -> None:
        try:
            LAYOUT.parent.mkdir(parents=True, exist_ok=True)
            LAYOUT.write_text(json.dumps({"top_h": self.top_h, "top_cuts": self.cuts, "worker_cut": self.wcut, "cleared": self.cleared,
                                          "size": list(self.screen.get_size())}))
        except OSError:
            pass

    def layout(self) -> dict[str, pygame.Rect]:
        """The panels and the dividers between them (v1, v2: between the top three; hz: above the talk
        and the worker; v3: between those two)."""
        w, h = self.screen.get_size()
        top, row = 66, w - 16
        th = max(MIN_TOP, min(self.top_h, h - top - GAP - MIN_TALK - 8))
        x1, x2 = 8 + round(row * self.cuts[0]), 8 + round(row * self.cuts[1])
        half = GAP // 2
        # keep every panel at least MIN_W wide when the window gets smaller (as far as it fits)
        x1 = max(x1, 8 + MIN_W + half)
        x2 = min(max(x2, x1 + MIN_W + GAP), w - 8 - MIN_W - half)
        x1 = min(x1, x2 - MIN_W - GAP)
        x3 = max(8 + MIN_W + half, min(8 + round(row * self.wcut), w - 8 - MIN_WORKER - half))
        y2, h2 = top + th + GAP, h - top - th - GAP - 8
        return {"screen": pygame.Rect(8, top, x1 - half - 8, th),
                "camera": pygame.Rect(x1 + half, top, x2 - x1 - GAP, th),
                "faces": pygame.Rect(x2 + half, top, w - 8 - x2 - half, th),
                "talk": pygame.Rect(8, y2, x3 - half - 8, h2),
                "worker": pygame.Rect(x3 + half, y2, w - 8 - x3 - half, h2 - SYSTEM_H - GAP),
                "system": pygame.Rect(x3 + half, y2 + h2 - SYSTEM_H, w - 8 - x3 - half, SYSTEM_H),
                "v1": pygame.Rect(x1 - half, top, GAP, th),
                "v2": pygame.Rect(x2 - half, top, GAP, th),
                "v3": pygame.Rect(x3 - half, y2, GAP, h2),
                "hz": pygame.Rect(8, top + th, w - 16, GAP)}

    def dragged(self, pos) -> None:
        w, h = self.screen.get_size()
        row, mx, my = w - 16, pos[0] - 8, pos[1]
        lo, hi = MIN_W + GAP, row - MIN_W - GAP
        if self.drag == "v1":
            self.cuts[0] = max(lo, min(mx, row * self.cuts[1] - MIN_W - GAP)) / row
        elif self.drag == "v2":
            self.cuts[1] = max(row * self.cuts[0] + MIN_W + GAP, min(mx, hi)) / row
        elif self.drag == "v3":
            self.wcut = max(MIN_W + GAP, min(mx, row - MIN_WORKER - GAP)) / row
        elif self.drag == "hz":
            self.top_h = max(MIN_TOP, min(my - 66 - GAP // 2, h - 66 - GAP - MIN_TALK - 8))

    def event(self, ev) -> None:
        if self.editing and self.edit_event(ev):
            return
        if ev.type == pygame.QUIT:
            self.running = False
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            pressed = next((k for k, b in self.buttons.items() if k.startswith("model:") and b.collidepoint(ev.pos)),
                           None) or next((k for k, b in self.buttons.items() if b.collidepoint(ev.pos)), None)
            if self.menu_open and not (pressed or "").startswith("model"):
                self.menu_open = False
            if pressed:
                self.press(pressed)
                return
            rects = self.layout()
            self.drag = next((k for k in DIVIDERS if rects[k].inflate(6, 6).collidepoint(ev.pos)), None)
        elif ev.type == pygame.MOUSEBUTTONUP and ev.button == 1:
            self.drag = None
        elif ev.type == pygame.MOUSEMOTION:
            if self.drag:
                self.dragged(ev.pos)
            self.set_cursor(ev.pos)
        elif ev.type == pygame.MOUSEWHEEL:
            self.scroll = max(0, self.scroll + ev.y * 40)
        elif ev.type == pygame.KEYDOWN:
            if ev.key in (pygame.K_q, pygame.K_ESCAPE):
                self.running = False
            elif ev.key == pygame.K_r:
                self.top_h, self.cuts, self.wcut = TOP_H, list(CUTS), WORKER_CUT
            elif ev.key == pygame.K_w:
                self.worker.toggle()
            elif ev.key == pygame.K_c:
                self.feed.toggle_camera()
                self.pictures.pop("camera", None)
            elif ev.key == pygame.K_PAGEUP:
                self.scroll += self.screen.get_height() // 2
            elif ev.key == pygame.K_PAGEDOWN:
                self.scroll = max(0, self.scroll - self.screen.get_height() // 2)
            elif ev.key == pygame.K_END:
                self.scroll = 0

    def set_cursor(self, pos) -> None:
        rects = self.layout()
        over = self.drag or next((k for k in DIVIDERS if rects[k].inflate(6, 6).collidepoint(pos)), None)
        if not over and any(b.collidepoint(pos) for b in self.buttons.values()):
            over = "button"
        cursor = {"v1": pygame.SYSTEM_CURSOR_SIZEWE, "v2": pygame.SYSTEM_CURSOR_SIZEWE,
                  "v3": pygame.SYSTEM_CURSOR_SIZEWE, "hz": pygame.SYSTEM_CURSOR_SIZENS,
                  "button": pygame.SYSTEM_CURSOR_HAND}.get(over, pygame.SYSTEM_CURSOR_ARROW)
        if cursor != getattr(self, "_cursor", None):
            self._cursor = cursor
            try:
                pygame.mouse.set_system_cursor(cursor)
            except pygame.error:
                pass

    def press(self, name: str) -> None:
        if name == "worker":
            self.worker.toggle()
        elif name in SETTINGS:
            key = SETTINGS[name]
            on = not now_state(self.feed.snapshot()[0])[name]
            self.wanted[name] = (on, time.monotonic() + 20)
            self.feed.set_setting(key, on)
        elif name == "model":
            self.menu_open = not self.menu_open
        elif name.startswith("model:"):
            self.menu_open = False
            self.model_wanted = (name[6:], time.monotonic() + 20)
            self.feed.set_setting("MODEL", name[6:])
        elif name == "power":
            if self.confirm and self.confirm[0] == "\0power" and time.monotonic() < self.confirm[1]:
                self.confirm, self.powering_off = None, time.monotonic()
                self.feed.power_off()
            else:
                self.confirm = ("\0power", time.monotonic() + 4)
        elif name == "delete":
            if self.confirm and self.confirm[0] == "\0conversation" and time.monotonic() < self.confirm[1]:
                self.confirm = None
                self.cleared, self.scroll = time.time(), 0
                self.feed.clear_conversation()
            else:
                self.confirm = ("\0conversation", time.monotonic() + 4)
        elif name.startswith("trash:"):
            who = name[6:]
            if self.confirm and self.confirm[0] == who and time.monotonic() < self.confirm[1]:
                self.confirm = None
                self.face_busy[who] = "deleting…"
                self.feed.faces_command(["forget", who], lambda error: self.face_done(who, error))
            else:
                self.confirm = (who, time.monotonic() + 4)
        elif name.startswith("edit:"):
            self.editing = {"old": name[5:], "text": name[5:]}
            pygame.key.start_text_input()

    def edit_event(self, ev) -> bool:
        """Typing a new name: Enter saves it, Esc (or a click elsewhere) cancels. True if it was used."""
        ed = self.editing
        if ev.type == pygame.TEXTINPUT:
            if len(ed["text"]) < 40:
                ed["text"] += ev.text
            return True
        if ev.type == pygame.MOUSEBUTTONDOWN:
            self.stop_editing()
            return False  # the click still does what it does
        if ev.type != pygame.KEYDOWN:
            return False
        if ev.key == pygame.K_ESCAPE:
            self.stop_editing()
        elif ev.key == pygame.K_BACKSPACE:
            ed["text"] = ed["text"][:-1]
        elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            old, new = ed["old"], " ".join(ed["text"].split())
            self.stop_editing()
            if new and new != old:
                self.face_busy[old] = "renaming…"
                self.feed.faces_command(["rename", old, new], lambda error: self.face_done(old, error))
        return True  # no shortcuts (q, c, w, r) while typing

    def stop_editing(self) -> None:
        self.editing = None
        pygame.key.stop_text_input()

    def face_done(self, name: str, error: str) -> None:
        """A delete or rename finished there (on the command's thread): the list shows it within 2 s."""
        if error:
            self.face_busy.pop(name, None)
            self.face_error = (error, time.monotonic() + 8)
        else:
            self.face_busy[name] = "done"

    def button(self, name: str, text: str, color, enabled: bool, right: int, y: int, h: int = 26) -> int:
        """A button ending at `right`; clicks on it go to press(name) while it's enabled. Returns its left."""
        img = self.small.render(text, True, BG if enabled else DIM)
        rect = pygame.Rect(right - img.get_width() - 24, y, img.get_width() + 24, h)
        hover = enabled and rect.collidepoint(pygame.mouse.get_pos())
        pygame.draw.rect(self.screen, tuple(min(255, c + 30) for c in color) if hover else color, rect, border_radius=6)
        self.screen.blit(img, img.get_rect(center=rect.center))
        if enabled:
            self.buttons[name] = rect
        else:
            self.buttons.pop(name, None)
        return rect.x

    def label(self, text: str, pos, font=None, color=TEXT) -> int:
        img = (font or self.font).render(text, True, color)
        self.screen.blit(img, pos)
        return pos[0] + img.get_width()

    def pill(self, text: str, x: int, y: int, color) -> int:
        img = self.small.render(text, True, BG)
        r = pygame.Rect(x, y, img.get_width() + 16, 22)
        pygame.draw.rect(self.screen, color, r, border_radius=11)
        self.screen.blit(img, (x + 8, y + 5))
        return r.right

    def draw(self) -> None:
        events, service, connected, error = self.feed.snapshot()
        w, h = self.screen.get_size()
        self.screen.fill(BG)
        st = now_state(events)
        self.draw_status(w, service, connected, error, st)
        r = self.layout()
        self.draw_picture("screen", r["screen"], st, connected)
        self.draw_picture("camera", r["camera"], st, connected)
        self.draw_faces(r["faces"], st, connected)
        self.draw_transcript(r["talk"], [row for row in transcript(events) if row[1] > self.cleared])
        self.draw_worker(r["worker"], st)
        self.draw_system(r["system"], connected)
        self.draw_menu()
        # the divider under the mouse, or being dragged
        mouse = pygame.mouse.get_pos()
        for k in DIVIDERS:
            if self.drag == k or (not self.drag and r[k].inflate(6, 6).collidepoint(mouse)):
                c = r[k].center
                if k == "hz":
                    pygame.draw.line(self.screen, COLOR, (r[k].x + 40, c[1]), (r[k].right - 40, c[1]), 2)
                else:
                    pygame.draw.line(self.screen, COLOR, (c[0], r[k].y + 20), (c[0], r[k].bottom - 20), 2)

    def graph(self, rect: pygame.Rect, values: list[float], n: int, color) -> None:
        """CPU % over time, filled under the line; the newest on the right, `n` samples across."""
        pygame.draw.rect(self.screen, BG, rect, border_radius=3)
        if len(values) < 2:
            return
        step = rect.width / (n - 1)
        pts = [(rect.right - (len(values) - 1 - i) * step, rect.bottom - 1 - min(100.0, v) / 100 * (rect.height - 2))
               for i, v in enumerate(values)]
        dim = tuple(c // 3 for c in color)
        pygame.draw.polygon(self.screen, dim, [(pts[0][0], rect.bottom - 1), *pts, (pts[-1][0], rect.bottom - 1)])
        pygame.draw.lines(self.screen, color, False, pts, 1)

    def bar(self, rect: pygame.Rect, share: float, color) -> None:
        pygame.draw.rect(self.screen, BG, rect, border_radius=3)
        if share > 0:
            pygame.draw.rect(self.screen, color, (rect.x, rect.y, max(2, round(rect.width * min(1.0, share))), rect.height),
                             border_radius=3)

    def machine(self, rect: pygame.Rect, name: str, cpu: list[float], n: int, mem: tuple[int, int]) -> None:
        """One machine across `rect`: its name, a CPU graph of the last minute (share of the whole machine)
        and a RAM bar, both as wide and the graph as tall as the row allows."""
        used, total = mem
        amount = f"{used / 1024:.0f} MB" if used < 1024 * 1024 else f"{used / 1024 / 1024:.1f}"
        ram = f"{amount} / {total / 1024 / 1024:.0f} GB" if total else "–"
        mid = rect.centery
        x = self.label(name, (rect.x, mid - 9), self.font, TEXT) if name else rect.x
        x = max(x, rect.x + 90) + 8
        pct_w, ram_w = self.small.size("100%")[0], self.small.size("000 MB / 00 GB")[0]
        room = rect.right - x - (36 + 8 + pct_w + 20) - (36 + 8 + ram_w)  # what the graph and bar share
        graph_w, bar_w = max(20, int(room * 0.62)), max(20, room - int(room * 0.62))
        self.label("CPU", (x, mid - 7), self.small, DIM)
        x += 36
        self.graph(pygame.Rect(x, rect.y + 2, graph_w, rect.height - 4), cpu, n, COLOR)
        x += graph_w + 8
        self.label(f"{cpu[-1]:3.0f}%" if cpu else "–", (x, mid - 7), self.small, TEXT)
        x += pct_w + 20
        self.label("RAM", (x, mid - 7), self.small, DIM)
        x += 36
        self.bar(pygame.Rect(x, mid - 5, bar_w, 10), used / total if total else 0, MOOD)
        x += bar_w + 8
        self.label(ram, (rect.right - self.small.size(ram)[0], mid - 7), self.small, DIM)

    def draw_status(self, w: int, service: dict, connected: bool, error: str, st: dict) -> None:
        pygame.draw.rect(self.screen, PANEL, (8, 8, w - 16, 50), border_radius=8)
        # switching hektor off, on the right: twice to confirm, then what happens
        sure = self.confirm and self.confirm[0] == "\0power" and time.monotonic() < self.confirm[1]
        if self.powering_off and time.monotonic() - self.powering_off < 120:
            power_x = self.button("power", "hektor is shutting down…" if connected else "hektor is off", LINE, False,
                                  w - 20, 20, 26)
        else:
            power_x = self.button("power", "shut down hektor?" if sure else "Shut down", WARN, connected, w - 20, 20, 26)
        self.screen.set_clip(pygame.Rect(8, 8, power_x - 20, 50))
        x = self.label("Hektor", (20, 20), self.title, COLOR) + 14
        x = self.label(self.host, (x, 24), self.small, DIM) + 16
        if not connected:
            self.pill("NO CONNECTION", x, 20, WARN)
            self.label(error[:90] if error else "connecting…", (x + 150, 24), self.small, DIM)
            self.screen.set_clip(None)
            return
        active = service.get("active", "?")
        x = self.pill("RUNNING" if active == "active" else active.upper() or "?", x, 20,
                      GOOD if active == "active" else WARN) + 12
        if active == "active":
            m = re.search(r"(\w{3}) \d{4}-\d\d-\d\d (\d\d:\d\d)", service.get("since", ""))
            if m:
                x = self.label(f"since {m.group(1)} {m.group(2)}", (x, 24), self.small, DIM)
        # what it's doing now, its mood, how long the last turn took
        state = st["state"] if active == "active" else "off"
        self.screen.set_clip(pygame.Rect(8, 8, power_x - 20, 50))
        x = max(x + 28, 420)
        x = self.label(state, (x, 17), self.title, STATES.get(state, TEXT)) + 28
        x = self.label("mood", (x, 24), self.small, DIM) + 8
        x = self.label(st["mood"] or "–", (x, 21), self.font, MOOD if st["mood"] else DIM) + 28
        turn = " · ".join(p for p in (f"whisper {st['stt']} s" if st["stt"] is not None else "",
                                      f"claude {st['claude']} s" if st["claude"] is not None else "",
                                      f"talking after {st['wait']} s" if st.get("wait") is not None else "") if p)
        x = self.label("last turn", (x, 24), self.small, DIM) + 8
        self.label(turn or "–", (x, 21), self.font, TEXT if turn else DIM)
        self.screen.set_clip(None)

    def draw_picture(self, name: str, area: pygame.Rect, st: dict, connected: bool) -> None:
        s = self.screen
        pygame.draw.rect(s, PANEL, area, border_radius=8)
        self.label(name, (area.x + 12, area.y + 10), self.small, DIM)
        if name == "camera":
            self.camera_buttons(area, st, connected)
        view = pygame.Rect(area.x + 12, area.y + 30, area.width - 24, area.height - 42)
        pygame.draw.rect(s, BG, view, border_radius=4)
        frame, have = self.feed.picture(name), self.pictures.get(name)
        if frame and (not have or have[0] != frame[0]):
            try:
                have = self.pictures[name] = (frame[0], pygame.image.load(io.BytesIO(frame[1]), f"{name}.jpg"))
            except pygame.error:
                pass
        if name == "camera" and st["camera"] is False:
            msg = "Hektor's camera is off"
        elif name == "camera" and not self.feed.camera:
            msg = "not shown here (c)"
        elif not have:
            msg = "no picture yet"
        else:
            img = have[1]
            scale = min(view.width / img.get_width(), view.height / img.get_height())
            img = pygame.transform.smoothscale(img, (int(img.get_width() * scale), int(img.get_height() * scale)))
            s.blit(img, img.get_rect(center=view.center))
            msg = "" if time.monotonic() - have[0] < 3 else "no new picture"
        if msg == "no new picture":
            self.label(msg, (view.x + 6, view.bottom - 18), self.small, WARN)
        elif msg:
            img = self.small.render(msg, True, DIM)
            s.blit(img, img.get_rect(center=view.center))

    def camera_buttons(self, area: pygame.Rect, st: dict, connected: bool) -> None:
        self.setting_button("camera", "camera", area.right - 10, area.y + 5, st, connected)

    def setting_button(self, name: str, label: str, right: int, y: int, st: dict, connected: bool) -> int:
        """A setting on hektor (camera, recognition) on or off; while the app applies it, what it does.
        Returns the button's left."""
        on = st[name]
        if name in self.wanted:
            want, until = self.wanted[name]
            if on == want or time.monotonic() > until:
                del self.wanted[name]
        if not connected or on is None:
            text, color, enabled = f"{label} ?", LINE, False
        elif name in self.wanted:
            text, color, enabled = f"{label}: turning {'on' if self.wanted[name][0] else 'off'}…", LINE, False
        else:
            text, color, enabled = f"{label}: {'on' if on else 'off'}", GOOD if on else LINE, True
        return self.button(name, text, color, enabled, right, y, 22)

    def draw_faces(self, area: pygame.Rect, st: dict, connected: bool) -> None:
        """Face detection (recognition) on or off, who it sees now, and who it knows."""
        s = self.screen
        pygame.draw.rect(s, PANEL, area, border_radius=8)
        x = area.x + 12
        self.label("faces", (x, area.y + 10), self.small, DIM)
        self.setting_button("recognition", "detect faces", area.right - 10, area.y + 5, st, connected)
        s.set_clip(area.inflate(-8, -8))
        # who it sees now
        y = area.y + 40
        self.label("sees", (x, y + 8), self.small, DIM)
        if not connected or st["camera"] is None:
            who, color = "–", DIM
        elif not st["camera"]:
            who, color = "camera off", DIM
        elif st["face"]:
            who = st["person"] or ("someone" if st["recognition"] else "a face")
            color = GOOD if st["person"] else TEXT
        else:
            who, color = "nobody", DIM
        self.label(who, (x + 46, y), self.title, color)
        # who it knows
        y += 46
        pygame.draw.line(s, LINE, (x, y), (area.right - 12, y))
        people = self.feed.known() if connected else None
        header = self.label(f"knows ({len(people)})" if people else "knows", (x, y + 8), self.small, DIM)
        if self.editing:
            self.label("·  new name: Enter saves, Esc cancels", (header + 10, y + 8), self.small, COLOR)
        y += 32
        if people is None:
            self.label("–", (x, y), self.small, DIM)
        elif not people:
            for i, line in enumerate(wrap('nobody yet: press f on hektor to add a face',
                                          self.small, area.width - 24)):
                self.label(line, (x, y + 18 * i), self.small, DIM)
        # the rows' buttons are drawn again below: none left over from someone deleted or renamed
        self.buttons = {k: b for k, b in self.buttons.items() if not k.startswith(("trash:", "edit:"))}
        # names that left the list (deleted, renamed) are done
        self.face_busy = {n: b for n, b in self.face_busy.items() if people is None or n in people}
        if self.editing and people is not None and self.editing["old"] not in people:
            self.stop_editing()
        for name, prints in sorted((people or {}).items(), key=lambda p: p[0].lower()):
            self.face_row(name, prints, name == st["person"], x, y, area.right - 12, connected)
            y += 28
        if self.face_error:
            if time.monotonic() < self.face_error[1]:
                for i, line in enumerate(wrap(self.face_error[0], self.small, area.width - 24)):
                    self.label(line, (x, y + 4 + 18 * i), self.small, WARN)
            else:
                self.face_error = None
        s.set_clip(None)

    def face_row(self, name: str, prints: int, here: bool, x: int, y: int, right: int, connected: bool) -> None:
        """One person Hektor knows: the name (a text field while renaming), and edit and delete buttons."""
        s = self.screen
        busy = self.face_busy.get(name)
        if self.editing and self.editing["old"] == name:
            field = pygame.Rect(x - 4, y - 2, right - x + 4, 26)
            pygame.draw.rect(s, BG, field, border_radius=4)
            pygame.draw.rect(s, COLOR, field, 1, border_radius=4)
            end = self.label(self.editing["text"], (x + 2, y + 1), self.font, TEXT)
            if int(time.monotonic() * 2) % 2:  # the cursor, blinking
                pygame.draw.line(s, COLOR, (end + 2, y + 2), (end + 2, y + 19), 2)
            return
        name_end = self.label(name, (x, y), self.font, GOOD if here else (DIM if busy else TEXT))
        # the buttons on the right: delete, then edit
        enabled = connected and not busy
        bx = right
        confirming = self.confirm and self.confirm[0] == name and time.monotonic() < self.confirm[1]
        if confirming:
            bx = self.button(f"trash:{name}", "delete?", WARN, enabled, bx, y - 1, 22) - 4
        else:
            bx = self.icon_button(f"trash:{name}", "trash", bx, y - 1, enabled) - 4
        bx = self.icon_button(f"edit:{name}", "edit", bx, y - 1, enabled and not confirming) - 10
        info = busy or ("here now" if here else f"{prints} fingerprints")
        if bx - self.small.size(info)[0] > name_end + 10:  # only where it fits
            self.label(info, (bx - self.small.size(info)[0], y + 4), self.small, GOOD if here and not busy else DIM)

    def icon_button(self, name: str, icon: str, right: int, y: int, enabled: bool) -> int:
        """A small square button with a drawn icon (trash, edit), ending at `right`. Returns its left."""
        rect = pygame.Rect(right - 22, y, 22, 22)
        hover = enabled and rect.collidepoint(pygame.mouse.get_pos())
        if hover:
            pygame.draw.rect(self.screen, WARN if icon == "trash" else LINE, rect, border_radius=5)
        c = (BG if icon == "trash" else TEXT) if hover else (DIM if enabled else LINE)
        cx, cy = rect.center
        if icon == "trash":  # a lid with a handle, a can with ribs
            pygame.draw.line(self.screen, c, (cx - 6, cy - 5), (cx + 6, cy - 5), 2)
            pygame.draw.line(self.screen, c, (cx - 2, cy - 7), (cx + 2, cy - 7), 2)
            pygame.draw.lines(self.screen, c, False, [(cx - 5, cy - 3), (cx - 4, cy + 7), (cx + 4, cy + 7), (cx + 5, cy - 3)], 2)
            for dx in (-2, 1):
                pygame.draw.line(self.screen, c, (cx + dx, cy - 1), (cx + dx, cy + 4), 1)
        else:  # a pencil, tip down-left
            pygame.draw.line(self.screen, c, (cx - 5, cy + 5), (cx + 5, cy - 5), 3)
            pygame.draw.line(self.screen, c, (cx - 7, cy + 7), (cx - 5, cy + 5), 1)
            pygame.draw.line(self.screen, c, (cx + 3, cy - 7), (cx + 7, cy - 3), 2)
        if enabled:
            self.buttons[name] = rect
        else:
            self.buttons.pop(name, None)
        return rect.x

    def draw_system(self, area: pygame.Rect, connected: bool) -> None:
        """CPU (the last minute, share of the whole machine) and RAM of hektor and monitor, filling the panel."""
        pygame.draw.rect(self.screen, PANEL, area, border_radius=8)
        self.screen.set_clip(area.inflate(-8, -8))
        self.label("system", (area.x + 12, area.y + 10), self.small, DIM)
        snap = self.worker.snapshot()
        cpu, mem = self.feed.system() if connected else ([], (0, 0))
        rows = (("hektor", cpu, 60, mem), ("monitor", snap["cpu"], self.worker.cpu.maxlen, snap["mem"]))
        top, h = area.y + 30, (area.height - 30 - 10) // len(rows)
        for i, (name, c, n, m) in enumerate(rows):
            self.machine(pygame.Rect(area.x + 12, top + i * h + 3, area.width - 26, h - 6), name, c, n, m)
        self.screen.set_clip(None)

    def draw_worker(self, area: pygame.Rect, st: dict) -> None:
        """The worker on monitor: running, busy, the button, CPU and RAM, the log."""
        s, wk = self.screen, self.worker
        snap = wk.snapshot()
        pygame.draw.rect(s, PANEL, area, border_radius=8)
        s.set_clip(area.inflate(-8, -8))
        x, y = area.x + 12, area.y + 10
        self.label("worker", (x, y), self.small, DIM)
        self.label("monitor", (x + 56, y), self.small, LINE)

        # the button, top right
        if not wk.installed:
            text, color, enabled = "not set up", LINE, False
        elif snap["acting"]:
            text, color, enabled = f"{snap['acting']}…", LINE, False
        elif wk.running:
            text, color, enabled = "Stop worker", WARN, True
        else:
            text, color, enabled = "Start worker", GOOD, True
        self.button("worker", text, color, enabled, area.right - 22, area.y + 8)

        # running, the tunnel, busy
        y += 34
        whisper, tunnel = wk.state(UNITS[0]), wk.state(UNITS[1])
        if not wk.installed:
            x2 = self.pill("NOT SET UP", x, y, LINE) + 12
            self.label("worker/setup.sh", (x2, y + 4), self.small, DIM)
        else:
            up = whisper == "active"
            x2 = self.pill("RUNNING" if up else whisper.upper(), x, y, GOOD if up else WARN if whisper == "failed" else LINE) + 12
            m = re.search(r"(\d\d:\d\d)", wk.since(UNITS[1]))
            tun = f"tunnel up since {m.group(1)}" if tunnel == "active" and m else f"tunnel {tunnel}"
            self.label(tun, (x2, y + 4), self.small, TEXT if tunnel == "active" else WARN if up else DIM)
        y += 34
        if snap["busy"]:
            x2 = self.pill("BUSY", x, y, BUSY) + 12
            self.label("transcribing…", (x2, y + 4), self.small, BUSY)
        else:
            x2 = self.pill("IDLE", x, y, LINE) + 12
            if snap["last"]:
                when, secs = snap["last"]
                self.label(f"last: {secs:.1f} s at {time.strftime('%H:%M:%S', time.localtime(when))}",
                           (x2, y + 4), self.small, DIM)
        if st.get("stt_on"):
            used = f"hektor's last question: whisper on the {st['stt_on']}, {st['stt']} s"
            self.label(used, (x, y + 30), self.small, GOOD if st["stt_on"] == "worker" else DIM)

        y += 58
        pygame.draw.line(s, LINE, (x, y), (area.right - 12, y))

        # the log, newest at the bottom
        inner = pygame.Rect(x, y + 6, area.width - 24, area.bottom - 10 - y - 6)
        s.set_clip(inner)
        rows: list[tuple[str, str]] = []
        for t, kind, text in sorted(snap["log"], key=lambda e: e[0])[-80:]:
            stamp = time.strftime("%H:%M:%S", time.localtime(t))
            for i, line in enumerate(wrap(text, self.small, inner.width - 70)):
                rows.append((stamp if i == 0 else "", line, kind))
        ly = inner.bottom - 18 * len(rows)
        for stamp, line, kind in rows:
            if ly + 18 >= inner.y:
                self.label(stamp, (inner.x, ly), self.small, LINE)
                self.label(line, (inner.x + 66, ly), self.small, LOG_COLORS.get(kind, DIM))
            ly += 18
        if not rows:
            self.label("nothing yet", (inner.x, inner.y), self.small, DIM)
        s.set_clip(None)

    def model_button(self, right: int, y: int) -> int:
        """Which Claude model Hektor asks, a menu to switch (MODEL on hektor). Returns its left."""
        st = now_state(self.feed.snapshot()[0])
        model, names = st["model"], {m: n for m, n, _ in MODELS}
        if self.model_wanted and (model == self.model_wanted[0] or time.monotonic() > self.model_wanted[1]):
            self.model_wanted = None
        if not self.feed.connected or model is None:
            text, enabled = "model ?", False
        elif self.model_wanted:
            text, enabled = f"model: {names.get(self.model_wanted[0], self.model_wanted[0])}…", False
        else:
            text, enabled = f"model: {names.get(model, model or 'default')}      ", True
        left = self.button("model", text, LINE, enabled, right, y, 22)
        if enabled:  # a small ▾
            r = self.buttons["model"]
            cx, cy = r.right - 14, r.centery
            pygame.draw.polygon(self.screen, BG, [(cx - 4, cy - 2), (cx + 4, cy - 2), (cx, cy + 3)])
        return left

    def draw_menu(self) -> None:
        """The open model menu, over everything, under its button."""
        self.buttons = {k: b for k, b in self.buttons.items() if not k.startswith("model:")}
        anchor = self.buttons.get("model")
        if not self.menu_open or not anchor:
            self.menu_open = False
            return
        current = now_state(self.feed.snapshot()[0])["model"]
        width = max(220, anchor.width)
        box = pygame.Rect(anchor.right - width, anchor.bottom + 4, width, 30 * len(MODELS) + 8)
        pygame.draw.rect(self.screen, PANEL, box, border_radius=8)
        pygame.draw.rect(self.screen, LINE, box, 1, border_radius=8)
        mouse = pygame.mouse.get_pos()
        for i, (model, name, note) in enumerate(MODELS):
            row = pygame.Rect(box.x + 4, box.y + 4 + 30 * i, box.width - 8, 30)
            if row.collidepoint(mouse):
                pygame.draw.rect(self.screen, LINE, row, border_radius=6)
            here = model == current
            if here:
                pygame.draw.circle(self.screen, COLOR, (row.x + 15, row.centery), 4)
            self.label(name, (row.x + 28, row.y + 5), self.font, COLOR if here else TEXT)
            if note:
                self.label(note, (row.right - 10 - self.small.size(note)[0], row.y + 8), self.small, DIM)
            self.buttons[f"model:{model}"] = row

    def draw_transcript(self, area: pygame.Rect, rows) -> None:
        s = self.screen
        pygame.draw.rect(s, PANEL, area, border_radius=8)
        self.label("conversation", (area.x + 12, area.y + 10), self.small, DIM)
        sure = self.confirm and self.confirm[0] == "\0conversation" and time.monotonic() < self.confirm[1]
        delete_x = self.button("delete", "delete here and on hektor?" if sure else "Delete", WARN if rows else LINE,
                               bool(rows), area.right - 10, area.y + 5, 22)
        delete_x = self.model_button(delete_x - 8, area.y + 5)
        inner = pygame.Rect(area.x + 12, area.y + 32, area.width - 24, area.height - 42)
        bubble_w = int(inner.width * 0.75)

        # lay out from the newest up, then draw what's in view
        blocks = []  # (height, draw function)
        for kind, t, label, text in rows:
            stamp = time.strftime("%H:%M:%S", time.localtime(t))
            if kind in ("you", "hektor"):
                lines = wrap(text, self.font, bubble_w - 24)
                bh = 30 + 22 * len(lines)
                blocks.append((bh + 8, (kind, stamp, label, lines, bh)))
            else:
                blocks.append((22, (kind, stamp, label, [text], 22)))
        total = sum(b[0] for b in blocks)
        self.scroll = min(self.scroll, max(0, total - inner.height))
        y = inner.bottom + self.scroll - total
        s.set_clip(inner)
        for height, (kind, stamp, label, lines, bh) in blocks:
            if y + height >= inner.y and y <= inner.bottom:
                if kind in ("you", "hektor"):
                    x = inner.x if kind == "you" else inner.right - bubble_w
                    color = LINE if kind == "you" else (18, 60, 56)
                    pygame.draw.rect(s, color, (x, y, bubble_w, bh), border_radius=10)
                    self.label(f"{stamp}  {label}", (x + 12, y + 8), self.small, DIM if kind == "you" else COLOR)
                    for i, line in enumerate(lines):
                        self.label(line, (x + 12, y + 28 + 22 * i))
                elif kind == "rule":
                    cy = y + 11
                    img = self.small.render(f"{lines[0]}  {stamp}", True, DIM)
                    cx = inner.centerx - img.get_width() // 2
                    pygame.draw.line(s, LINE, (inner.x, cy), (cx - 10, cy))
                    pygame.draw.line(s, LINE, (cx + img.get_width() + 10, cy), (inner.right, cy))
                    s.blit(img, (cx, y + 5))
                else:
                    self.label(f"{stamp}  {lines[0]}", (inner.x + 4, y + 4), self.small,
                               WARN if kind == "error" else DIM)
            y += height
        s.set_clip(None)
        if not rows:
            self.label("nothing yet", (inner.x + 4, inner.y + 4), self.small, DIM)
        if self.scroll:
            self.pill("End: back to the latest", delete_x - 190, area.y + 6, COLOR)


def default_host() -> str:
    """$HEKTOR_HOST, else HEKTOR_HOST in deploy.conf."""
    host = os.environ.get("HEKTOR_HOST")
    if not host and DEPLOY_CONF.exists():
        for line in DEPLOY_CONF.read_text().splitlines():
            if line.startswith("HEKTOR_HOST="):
                host = line.split("=", 1)[1].strip()
    if not host:
        raise SystemExit("no host to watch: give one (dev/run.sh monitor user@machine), or copy deploy.conf.example "
                         "to deploy.conf and fill it in")
    return host


def run(host: str | None = None) -> None:
    host = host or default_host()
    pygame.display.init()
    pygame.font.init()
    mon = Monitor(host)
    try:
        mon.run()
    except KeyboardInterrupt:
        mon.feed.stop()
    pygame.quit()
