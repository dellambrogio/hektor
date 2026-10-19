"""screenshots: the pictures in doc/, made off-screen: the eyes in each state and mood, with the voice
stripe, and the monitor app with demo data (no camera picture, conversation, names or machines of anyone's:
a drawn stand-in for the camera, a made-up conversation). Nothing is played or opened.

    dev/run.sh screenshots      writes doc/eyes.png and doc/monitor.png
"""
import os
import threading
import time
from collections import deque
from pathlib import Path

os.environ["SDL_VIDEODRIVER"] = "dummy"
import pygame  # noqa: E402

from hektor.eyes import COLOR, Eyes  # noqa: E402
from hektor.stripe import Stripe  # noqa: E402

DOC = Path(__file__).resolve().parents[1] / "doc"
FACE = (640, 360)  # hektor's screen is 16:9; the app draws at this size and scales up
SHOTS = [  # (caption, state, mood, stripe, voice level)
    ("idle", "idle", None, None, 0.0),
    ("listening", "listening", None, "listening", 0.75),
    ("thinking: whisper", "thinking", None, "parsing", 0.0),
    ("thinking: Claude", "thinking", None, "generating", 0.0),
    ("speaking", "speaking", None, "speaking", 0.9),
    ("happy", "idle", "happy", None, 0.0),
    ("surprised", "idle", "surprised", None, 0.0),
    ("confused", "idle", "confused", None, 0.0),
    ("sad", "idle", "sad", None, 0.0),
    ("angry", "idle", "angry", None, 0.0),
    ("sleeping", "sleeping", None, None, 0.0),
]


def face(state: str, mood: str | None, stripe_state: str | None, level: float, conf) -> pygame.Surface:
    """The face as the app draws it, settled (no blink, looking ahead), with the stripe if wanted."""
    eyes, stripe = Eyes(), Stripe(conf)
    eyes.set_state(state)
    eyes.look_at((0.0, 0.0))
    if mood:
        eyes.emote(mood, hold=60)
    eyes.level = level
    for _ in range(90):  # 1.5 s at 60 fps: the expression and the stripe settle
        eyes.next_blink = eyes.time + 10
        eyes.update(1 / 60)
        stripe.update(1 / 60, stripe_state or "idle", level)
    surface = pygame.Surface(FACE)
    eyes.draw(surface, glow=True)
    if stripe_state:
        stripe.draw(surface)
    return surface


def eyes_sheet(conf) -> pygame.Surface:
    """All the faces on one picture, four across, captioned."""
    font = pygame.font.Font(None, 30)
    w, h, gap, cols = FACE[0] // 2, FACE[1] // 2, 16, 4
    rows = -(-len(SHOTS) // cols)
    sheet = pygame.Surface((cols * w + (cols + 1) * gap, rows * (h + 34) + (rows + 1) * gap - 10))
    sheet.fill((14, 16, 20))
    for i, (caption, state, mood, stripe_state, level) in enumerate(SHOTS):
        x, y = gap + (i % cols) * (w + gap), gap + (i // cols) * (h + 34 + gap)
        sheet.blit(pygame.transform.smoothscale(face(state, mood, stripe_state, level, conf), (w, h)), (x, y))
        pygame.draw.rect(sheet, (40, 44, 52), (x, y, w, h), 1)
        sheet.blit(font.render(caption, True, (170, 176, 186)), (x + 2, y + h + 8))
    return sheet


def camera_stand_in() -> bytes:
    """A drawn room with a drawn person, the face boxed like the app does: no one's real picture."""
    import cv2
    import numpy as np
    s = pygame.Surface((320, 240))
    for y in range(240):  # a wall, then a desk
        c = 70 + y // 6 if y < 170 else 50
        pygame.draw.line(s, (c, c - 4, c - 10) if y < 170 else (90, 70, 50), (0, y), (320, y))
    pygame.draw.rect(s, (120, 140, 160), (210, 30, 70, 90))         # a window
    pygame.draw.ellipse(s, (60, 70, 110), (110, 150, 110, 120))     # shoulders
    pygame.draw.ellipse(s, (210, 170, 140), (128, 70, 74, 92))      # a head
    pygame.draw.ellipse(s, (70, 50, 40), (124, 60, 82, 40))         # hair
    for ex in (150, 178):
        pygame.draw.circle(s, (40, 40, 40), (ex, 112), 4)
    pygame.draw.arc(s, (120, 60, 60), (148, 128, 34, 16), 3.5, 5.9, 2)
    pygame.draw.rect(s, (32, 230, 200), (122, 66, 86, 100), 2)      # what YuNet found
    rgb = np.transpose(pygame.surfarray.array3d(s), (1, 0, 2))
    return cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))[1].tobytes()


class DemoFeed:
    """Stands in for monitor.Feed: what hektor would send, made up."""

    def __init__(self, events, screen_jpg, camera_jpg) -> None:
        self.lock = threading.Lock()
        self.events, self.connected, self.camera = events, True, True
        self.service = {"type": "service", "active": "active", "since": "Mon 2026-10-19 18:02:11 CEST"}
        self.cpu = [28 + 10 * abs(((i * 7) % 23) - 11) / 11 + (25 if 40 <= i <= 44 else 0) for i in range(60)]
        self.frames = {"screen": (time.monotonic(), screen_jpg), "camera": (time.monotonic(), camera_jpg)}

    def snapshot(self):
        return list(self.events), dict(self.service), True, ""

    def system(self):
        return list(self.cpu), (3_700_000, 15_600_000)

    def known(self):
        return {"Anna": 12, "Marco": 11}

    def picture(self, name="camera"):
        return (time.monotonic(), self.frames[name][1])

    def set_setting(self, *a): pass
    def faces_command(self, *a): pass
    def clear_conversation(self): pass
    def toggle_camera(self): pass
    def stop(self): pass


class DemoWorker:
    """Stands in for dev.worker.WorkerWatch: monitor running the worker, made up."""
    installed, running = True, True

    def __init__(self) -> None:
        self.cpu = deque((4 + (30 if 150 <= i <= 153 else 0) + (i * 13) % 5 for i in range(240)), maxlen=240)

    def state(self, unit):
        return "active"

    def since(self, unit):
        return "Mon 2026-10-19 18:05:40 CEST"

    def snapshot(self):
        t = demo_time(0)
        log = [(t - 900, "info", "whisper: Started hektor-whisper.service - Hektor worker: whisper-server."),
               (t - 899, "info", "tunnel: Started hektor-tunnel.service - Hektor worker: tunnel to hektor."),
               (t - 160, "request", "whisper: request: 2.1 s of speech"), (t - 159, "done", "transcribed in 0.4 s"),
               (t - 95, "request", "whisper: request: 3.4 s of speech"), (t - 94, "done", "transcribed in 0.5 s"),
               (t - 40, "request", "whisper: request: 1.8 s of speech"), (t - 39, "done", "transcribed in 0.4 s")]
        return {"cpu": list(self.cpu), "mem": (6_100_000, 15_600_000), "busy": False, "last": (t - 39, 0.4),
                "log": log, "acting": ""}

    def toggle(self): pass
    def stop(self): pass


def demo_time(seconds_ago: float) -> float:
    return time.mktime((2026, 10, 19, 18, 42, 0, 0, 0, -1)) - seconds_ago


def demo_events() -> list[dict]:
    """A made-up evening with Hektor."""
    t = demo_time
    return [
        {"t": t(2400), "type": "start"}, {"t": t(2400), "type": "camera", "on": True},
        {"t": t(2400), "type": "recognition", "on": True}, {"t": t(2400), "type": "model", "name": "claude-haiku-5-5"},
        {"t": t(200), "type": "face", "seen": True}, {"t": t(199), "type": "person", "name": "Anna"},
        {"t": t(197), "type": "looked", "name": "Anna"},
        {"t": t(190), "type": "heard", "text": "What's on my calendar tomorrow?", "lang": "en", "stt": 0.4, "stt_on": "worker"},
        {"t": t(184), "type": "reply", "text": "Let me check. You have two things tomorrow: a dentist appointment at "
                                               "nine and lunch with Marco at half past twelve.",
         "mood": "neutral", "claude": 5.8, "wait": 1.9, "new": True},
        {"t": t(100), "type": "heard", "text": "Can you remind me what the weather will be like?", "lang": "en",
         "stt": 0.5, "stt_on": "worker"},
        {"t": t(93), "type": "reply", "text": "Let me look that up. Tomorrow looks mostly sunny and around eighteen "
                                              "degrees, so the walk to lunch should be nice.",
         "mood": "happy", "claude": 7.2, "wait": 1.6, "new": False},
        {"t": t(45), "type": "heard", "text": "Thanks Hektor!", "lang": "en", "stt": 0.4, "stt_on": "worker"},
        {"t": t(42), "type": "reply", "text": "Anytime, Anna! Enjoy your evening.", "mood": "happy", "claude": 2.1,
         "wait": 1.4, "new": False},
        {"t": t(41), "type": "mood", "mood": "happy"}, {"t": t(30), "type": "nothing", "seconds": 6.0, "why": "follow-up"},
        {"t": t(29), "type": "state", "state": "idle"},
    ]


def monitor_shot(conf) -> pygame.Surface:
    import cv2
    import numpy as np
    from dev import monitor
    eyes = face("idle", "happy", None, 0.0, conf)
    rgb = np.transpose(pygame.surfarray.array3d(eyes), (1, 0, 2))
    screen_jpg = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))[1].tobytes()
    monitor.Feed = lambda host: DemoFeed(demo_events(), screen_jpg, camera_stand_in())
    monitor.WorkerWatch = DemoWorker
    mon = monitor.Monitor("hektor@hektor")
    mon.screen = pygame.display.set_mode((1400, 860))
    mon.top_h, mon.cuts, mon.wcut, mon.cleared = monitor.TOP_H, list(monitor.CUTS), monitor.WORKER_CUT, 0.0
    mon.draw()
    return mon.screen.copy()


def run() -> None:
    from hektor import config
    pygame.display.init()
    pygame.font.init()
    pygame.display.set_mode((1, 1))
    conf = config.load()
    DOC.mkdir(exist_ok=True)
    for name, picture in (("eyes.png", eyes_sheet(conf)), ("monitor.png", monitor_shot(conf))):
        pygame.image.save(picture, str(DOC / name))
        print(DOC / name, f"{(DOC / name).stat().st_size // 1024} KB")
    pygame.quit()
