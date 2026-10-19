"""Procedural eyes.

Each eye is a rounded rectangle described by a handful of numbers (Eye); the face (Face) is two eyes
plus a whole-face offset, scale and brightness. Expressions are Face presets, and everything moves by
tweening between them. On top of the expression, Eyes adds the live parts: gaze, blinks, breathing
while asleep, jitter while thinking, bouncing with the voice.

Coordinates are in face units: 1 = the height of the canvas, (0, 0) its center, y going down.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, fields, replace

import pygame

COLOR = (32, 230, 200)   # teal
EYE_W, EYE_H = 0.36, 0.44  # rest size of an eye
EYE_GAP = 0.31           # rest distance of each eye center from the middle
GAZE_X, GAZE_Y = 0.22, 0.14  # how far the eyes move for a gaze of 1
SUPERSAMPLE = 2          # drawn this much bigger, then scaled down: antialiasing
GLOW = 150               # halo strength around the eyes, 0-255


@dataclass
class Eye:
    x: float = 0.0          # offset from the rest position
    y: float = 0.0
    w: float = 1.0          # scale of the rest size
    h: float = 1.0
    radius: float = 0.3     # corner radius, fraction of the shorter side
    lid_top: float = 0.0    # fraction of the height covered from above
    lid_angle: float = 0.0  # degrees; >0 lowers the inner corner of the top lid (angry), <0 the outer (sad)
    lid_bottom: float = 0.0  # fraction covered from below by a curved lid: happy ^ ^


@dataclass
class Face:
    left: Eye = field(default_factory=Eye)   # the eye on the left of the screen
    right: Eye = field(default_factory=Eye)
    x: float = 0.0
    y: float = 0.0
    scale: float = 1.0
    bright: float = 1.0


def face(both: dict | None = None, left: dict | None = None, right: dict | None = None, **kw) -> Face:
    """A Face preset: `both` applies to both eyes, `left`/`right` add to it per eye."""
    return Face(left=Eye(**{**(both or {}), **(left or {})}),
                right=Eye(**{**(both or {}), **(right or {})}), **kw)


EXPRESSIONS: dict[str, Face] = {
    "neutral":   face(),
    "happy":     face(both=dict(lid_bottom=0.45, w=1.05, y=-0.02)),
    "sad":       face(both=dict(lid_top=0.3, lid_angle=-22, h=0.85, y=0.05)),
    "surprised": face(both=dict(w=1.12, h=1.25, radius=0.4)),
    "confused":  face(left=dict(lid_top=0.4, lid_angle=8, h=0.9, y=0.02), right=dict(w=1.08, h=1.1, y=-0.03)),
    "angry":     face(both=dict(lid_top=0.3, lid_angle=24)),
    "sleeping":  face(both=dict(h=0.08, w=1.08, y=0.1, radius=0.5), bright=0.5),
    "listening": face(both=dict(w=1.06, h=1.12), scale=1.03),
    "thinking":  face(both=dict(lid_top=0.12, h=0.92), left=dict(lid_top=0.22)),
}
EMOTIONS = ["happy", "sad", "surprised", "confused", "angry"]
STATES = {"idle": "neutral", "sleeping": "sleeping", "listening": "listening",
          "thinking": "thinking", "speaking": "neutral"}


def lerp(a, b, t: float):
    if isinstance(a, (Eye, Face)):
        return type(a)(**{f.name: lerp(getattr(a, f.name), getattr(b, f.name), t) for f in fields(a)})
    if isinstance(a, tuple):
        return tuple(lerp(x, y, t) for x, y in zip(a, b))
    return a + (b - a) * t


def ease_in_out(t: float) -> float:
    return 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


class Tween:
    """A value moving from where it is to a target over `dur` seconds."""

    def __init__(self, value):
        self.start = self.end = value
        self.t, self.dur, self.ease = 1.0, 1.0, ease_in_out

    def to(self, value, dur: float, ease=ease_in_out) -> None:
        self.start, self.end = self.value, value
        self.t, self.dur, self.ease = 0.0, max(dur, 1e-3), ease

    def update(self, dt: float) -> None:
        self.t = min(self.t + dt, self.dur)

    @property
    def value(self):
        return lerp(self.start, self.end, self.ease(self.t / self.dur))


def blink_closed(t: float) -> float:
    """How closed the eyes are t seconds into a blink: 0 open, 1 shut, None when it's over."""
    close, hold, open_ = 0.06, 0.04, 0.12
    if t < close:
        return t / close
    if t < close + hold:
        return 1.0
    if t < close + hold + open_:
        return 1 - ease_out((t - close - hold) / open_)
    return None


class Eyes:
    """The live face: set the state, show emotions, look at things, bounce with the voice."""

    def __init__(self) -> None:
        self.state = "idle"
        self.time = 0.0
        self.expr = Tween(EXPRESSIONS["neutral"])
        self.shown = "neutral"           # expression self.expr is heading to
        self.emotion, self.emotion_until = None, 0.0
        self.gaze = Tween((0.0, 0.0))
        self.target: tuple[float, float] | None = None
        self.next_glance = 1.0
        self.blink_t: float | None = None
        self.next_blink = 2.0
        self.double_blink = False
        self.level, self._level = 0.0, 0.0
        self.face = EXPRESSIONS["neutral"]
        self._big: pygame.Surface | None = None
        self._scanlines: pygame.Surface | None = None

    # --- control --------------------------------------------------------------------------------

    def set_state(self, state: str) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        if state != self.state:
            self.state = state
            self.next_glance = self.time + 0.3

    def emote(self, name: str, hold: float = 2.5) -> None:
        """Show an expression for `hold` seconds, then go back to the state's own."""
        if name not in EXPRESSIONS:
            raise ValueError(f"unknown expression {name!r}")
        self.emotion, self.emotion_until = name, self.time + hold

    def look_at(self, target: tuple[float, float] | None) -> None:
        """Where to look, -1..1 each way (0, 0 = straight ahead); None to look around on its own."""
        self.target = target

    def blink(self) -> None:
        if self.blink_t is None:
            self.blink_t = 0.0

    def glance(self) -> None:
        """Look somewhere else now."""
        self.next_glance = self.time

    # --- animation ------------------------------------------------------------------------------

    def update(self, dt: float) -> None:
        self.time += dt
        now = self.time

        if self.emotion and now >= self.emotion_until:
            self.emotion = None
        wanted = self.emotion or STATES[self.state]
        if wanted != self.shown:
            self.shown = wanted
            self.expr.to(EXPRESSIONS[wanted], 0.6 if "sleeping" in (wanted, self.state) else 0.25)
        self.expr.update(dt)

        self._update_gaze(now)
        self.gaze.update(dt)
        closed = self._update_blink(dt, now)
        self._level += (self.level - self._level) * min(1.0, dt * 18)

        f = self.expr.value
        gx, gy = self.gaze.value
        left, right = f.left, f.right
        # the eye on the side we look towards is closer, so a bit bigger
        left = replace(left, h=left.h * (1 - 0.08 * gx), w=left.w * (1 - 0.04 * gx))
        right = replace(right, h=right.h * (1 + 0.08 * gx), w=right.w * (1 + 0.04 * gx))
        fx, fy, bright = f.x + gx * GAZE_X, f.y + gy * GAZE_Y, f.bright

        if self.state == "sleeping":
            breath = math.sin(now * 2 * math.pi / 4.5)
            left, right = (replace(e, h=e.h * (1 + 0.35 * breath)) for e in (left, right))
            bright *= 1 + 0.15 * breath
        elif self.state == "thinking":
            fx += 0.004 * math.sin(now * 31) + 0.003 * math.sin(now * 17)
        elif self.state == "speaking":
            lvl = self._level
            left, right = (replace(e, h=e.h * (1 + 0.18 * lvl), w=e.w * (1 - 0.04 * lvl)) for e in (left, right))
            fy -= 0.025 * lvl
        if closed:
            left, right = (replace(e, h=e.h * (1 - 0.92 * closed)) for e in (left, right))

        self.face = Face(left=left, right=right, x=fx, y=fy, scale=f.scale, bright=bright)

    def _update_gaze(self, now: float) -> None:
        if self.state == "sleeping":
            if self.gaze.end != (0.0, 0.0):
                self.gaze.to((0.0, 0.0), 0.8)
            return
        if self.target is not None:
            # jump to the target like a saccade when it moved enough, otherwise stay put
            tx, ty = self.target
            ex, ey = self.gaze.end
            if math.hypot(tx - ex, ty - ey) > 0.06:
                self.gaze.to((tx, ty), 0.12, ease_out)
            return
        if now < self.next_glance:
            return
        if self.state == "thinking":
            # look up and to one side, then the other
            side = -1 if self.gaze.end[0] > 0 else 1
            self.gaze.to((side * random.uniform(0.4, 0.7), random.uniform(-0.8, -0.5)), 0.25)
            self.next_glance = now + random.uniform(0.9, 1.8)
        elif self.state in ("listening", "speaking"):
            # mostly at you, small glances
            self.gaze.to((random.uniform(-0.15, 0.15), random.uniform(-0.1, 0.1)), 0.1, ease_out)
            self.next_glance = now + random.uniform(1.5, 4)
        else:
            target = (0.0, 0.0) if random.random() < 0.35 else \
                (random.uniform(-0.8, 0.8), random.uniform(-0.6, 0.6))
            self.gaze.to(target, random.uniform(0.08, 0.14), ease_out)
            self.next_glance = now + random.uniform(0.8, 4)

    def _update_blink(self, dt: float, now: float) -> float:
        if self.blink_t is None:
            if self.state != "sleeping" and now >= self.next_blink:
                self.blink_t = 0.0
                self.double_blink = random.random() < 0.15
                lo, hi = (4, 9) if self.state == "listening" else (2, 8)
                self.next_blink = now + random.uniform(lo, hi)
            else:
                return 0.0
        self.blink_t += dt
        closed = blink_closed(self.blink_t)
        if closed is None:
            if self.double_blink:
                self.double_blink, self.blink_t = False, -0.08
                return 0.0
            self.blink_t = None
            return 0.0
        return max(closed, 0.0)

    # --- drawing --------------------------------------------------------------------------------

    def draw(self, surface: pygame.Surface, glow: bool = True, scanlines: bool = False) -> None:
        """Draw the current face filling `surface`."""
        size = surface.get_size()
        big_size = (size[0] * SUPERSAMPLE, size[1] * SUPERSAMPLE)
        if self._big is None or self._big.get_size() != big_size:
            self._big = pygame.Surface(big_size)
        big = self._big
        big.fill((0, 0, 0))

        f = self.face
        unit = big_size[1] * f.scale
        color = tuple(min(255, round(c * f.bright)) for c in COLOR)
        cx, cy = big_size[0] / 2 + f.x * unit, big_size[1] / 2 + f.y * unit
        _draw_eye(big, f.left, cx - EYE_GAP * unit, cy, unit, inner=1, color=color)
        _draw_eye(big, f.right, cx + EYE_GAP * unit, cy, unit, inner=-1, color=color)

        pygame.transform.smoothscale(big, size, surface)
        if glow:
            small = pygame.transform.smoothscale(surface, (max(1, size[0] // 10), max(1, size[1] // 10)))
            halo = pygame.transform.smoothscale(small, size)
            halo.fill((GLOW,) * 3, special_flags=pygame.BLEND_MULT)
            # max, not add: the halo only shows around the eyes and leaves their color alone
            surface.blit(halo, (0, 0), special_flags=pygame.BLEND_MAX)
        if scanlines:
            if self._scanlines is None or self._scanlines.get_size() != size:
                self._scanlines = pygame.Surface(size, pygame.SRCALPHA)
                for y in range(0, size[1], 2):
                    pygame.draw.line(self._scanlines, (0, 0, 0, 110), (0, y), (size[0], y))
            surface.blit(self._scanlines, (0, 0))


def _draw_eye(surf: pygame.Surface, eye: Eye, cx: float, cy: float, unit: float,
              inner: int, color: tuple[int, int, int]) -> None:
    """One eye centered at (cx, cy) in pixels; `inner` is +1 when its inner side is on the right."""
    w, h = EYE_W * eye.w * unit, EYE_H * eye.h * unit
    cx, cy = cx + eye.x * unit, cy + eye.y * unit
    rect = pygame.Rect(0, 0, max(1, round(w)), max(1, round(h)))
    rect.center = (round(cx), round(cy))
    pygame.draw.rect(surf, color, rect, border_radius=round(eye.radius * min(rect.w, rect.h)))

    surf.set_clip(rect)
    if eye.lid_top > 0 or eye.lid_angle:
        # black above a line through the lid height, tilted so the inner corner drops by `drop`
        lid_y = rect.top + eye.lid_top * h
        drop = math.tan(math.radians(eye.lid_angle)) * w / 2
        xi, xo = cx + inner * (w / 2 + 1), cx - inner * (w / 2 + 1)
        top = rect.top - 2
        pygame.draw.polygon(surf, (0, 0, 0), [(xi, lid_y + drop), (xo, lid_y - drop), (xo, top), (xi, top)])
    if eye.lid_bottom > 0:
        # an ellipse rising from below: its curved top leaves the eye arched like ^
        ew, eh = w * 1.6, h * 1.2
        pygame.draw.ellipse(surf, (0, 0, 0), pygame.Rect(cx - ew / 2, rect.bottom - eye.lid_bottom * h, ew, eh))
    surf.set_clip(None)
