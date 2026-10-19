"""The voice stripe under the eyes, like KITT's scanner: a row of segments that shows the conversation.

  listening   lights up from the middle with your voice: it hears you
  parsing     a light sweeping back and forth with a fading trail: whisper works out what you said
  generating  the same sweep in another color: Claude answers and the voice is made
  speaking    lights up from the middle with Hektor's voice

Each has its own color (STRIPE_* in hektor.conf).

It fades in when a conversation starts and out when Hektor is idle or asleep.
"""
import math

import pygame

from .config import Conf
from .eyes import COLOR

SWEEPING = ("parsing", "generating")
N = 11                 # segments: wide, flat bars like KITT's
WIDTH, HEIGHT = 0.7, 0.011  # of the canvas width / height; it sits on the bottom edge
FILL = 0.88            # of each segment's slot; the rest is the gap
DIM = 0.1              # brightness of an unlit segment
SWEEP = 1.6            # seconds for the scanner to go across and back


class Stripe:
    def __init__(self, conf: Conf) -> None:
        self.colors = {state: _rgb(conf[f"STRIPE_{state.upper()}"])
                       for state in ("listening", "parsing", "generating", "speaking")}
        self.state = "idle"
        self.alpha = 0.0            # fade in / out, 0-1
        self.level = 0.0            # smoothed voice level, 0-1
        self.phase = 0.0            # scanner position over time
        self.trail = [0.0] * N      # scanner brightness per segment

    def update(self, dt: float, state: str, level: float) -> None:
        """Every frame: Hektor's state (thinking: parsing or generating), and the voice level (yours
        listening, its own speaking), 0-1."""
        if state == "thinking":  # with the dev keys: no voice to say which
            state = "generating"
        busy = state in self.colors
        if busy:
            self.state = state  # while fading out it keeps its last look
        self.alpha = min(1.0, self.alpha + dt * 5) if busy else max(0.0, self.alpha - dt * 3)
        # quick to rise, slower to fall, like a VU meter
        rate = 25 if level > self.level else 6
        self.level += (level - self.level) * min(1.0, dt * rate)
        decay = math.exp(-dt * 7)
        self.trail = [b * decay for b in self.trail]
        if self.state in SWEEPING:
            self.phase += dt * 2 * math.pi / SWEEP
            pos = (1 - math.cos(self.phase)) / 2 * (N - 1)  # slows down at the ends, like KITT
            i = round(pos)
            self.trail[i] = 1.0
        else:
            self.phase = 0.0

    def brightness(self, i: int) -> float:
        if self.state in SWEEPING:
            return self.trail[i]
        # from the middle out: segment i is lit when the level reaches its distance, with a soft edge
        mid = (N - 1) / 2
        d = abs(i - mid) / mid
        return max(0.0, min(1.0, (self.level * 1.15 - d) * 4))

    def draw(self, surface: pygame.Surface) -> None:
        if self.alpha <= 0.01:
            return
        w, h = surface.get_size()
        seg_w = w * WIDTH / N
        sh = max(2, round(h * HEIGHT))
        x0, y = (w - w * WIDTH) / 2, h - sh
        base = self.colors.get(self.state, COLOR)
        for i in range(N):
            b = (DIM + (1 - DIM) * self.brightness(i)) * self.alpha
            color = tuple(round(c * b) for c in base)
            rect = pygame.Rect(round(x0 + i * seg_w + seg_w * (1 - FILL) / 2), y, max(2, round(seg_w * FILL)), sh)
            pygame.draw.rect(surface, color, rect, border_radius=1)


def _rgb(color: str) -> tuple[int, int, int]:
    """#rrggbb → (r, g, b)"""
    c = color.strip().lstrip("#")
    return int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)
