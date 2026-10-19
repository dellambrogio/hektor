"""Developer console: try Hektor's noises, the mic and the voice without talking to it.

Left: every noise, click one to play it (a new variation each click).
Right: the mic level live, against the background noise and the speech threshold the recorder
would use; below, type a message and press Enter (or Speak) to hear it with the current voice.

Keys: Enter speak, Esc stop talking (or quit when quiet), Ctrl+Q quit.
"""
import logging
import math
import subprocess
import threading
import time
from collections import deque

import pygame

from hektor.audio_in import EC_MIC, Mic, pw_node, rms
from hektor.config import RUNTIME, Conf
from hektor.eyes import COLOR
from hektor.sounds import IDLE, SOUNDS, make, write_wav
from hektor.tts import Speaker, pw_play

log = logging.getLogger(__name__)

SIZE = (960, 600)
BG, PANEL, LINE, TEXT, DIM = (12, 14, 16), (24, 28, 32), (48, 54, 60), (220, 226, 230), (120, 130, 138)
SPEECH, WARN = (240, 190, 60), (230, 90, 80)
GROUPS = [("conversation", ["wake", "ack", "dismiss"]),
          ("moods", ["happy", "sad", "surprised", "confused", "angry"]),
          ("sleep", ["yawn", "wakeup", "snore"]),
          ("idle", IDLE)]
ROW = 26
HISTORY = 100  # mic chunks on the graph: 10 s


def _meter(level: float) -> float:
    """Mic level 0-32768 to 0-1, on a log scale like mic-test."""
    return min(1.0, math.log10(1 + level) / math.log10(32768))


class Button:
    def __init__(self, rect: pygame.Rect, label: str) -> None:
        self.rect, self.label = rect, label

    def draw(self, screen, font, mouse, enabled: bool = True) -> None:
        hover = enabled and self.rect.collidepoint(mouse)
        pygame.draw.rect(screen, LINE if hover else PANEL, self.rect, border_radius=6)
        pygame.draw.rect(screen, COLOR if enabled else LINE, self.rect, 1, border_radius=6)
        img = font.render(self.label, True, TEXT if enabled else DIM)
        screen.blit(img, img.get_rect(center=self.rect.center))


class DevTool:
    def __init__(self, conf: Conf) -> None:
        self.conf = conf
        self.screen = pygame.display.set_mode(SIZE)
        pygame.display.set_caption("Hektor dev console")
        self.font = pygame.font.Font(None, 24)
        self.small = pygame.font.Font(None, 19)
        self.big = pygame.font.Font(None, 30)

        # sounds, grouped; anything new in sounds.py ends up under "other"
        listed = {n for _, names in GROUPS for n in names}
        groups = GROUPS + ([("other", [n for n in SOUNDS if n not in listed])] if set(SOUNDS) - listed else [])
        self.rows: list[tuple[str, str | None, pygame.Rect]] = []  # (label, sound or None for a heading, rect)
        y = 56
        for title, names in groups:
            self.rows.append((title, None, pygame.Rect(16, y, 268, ROW - 6)))
            y += ROW - 6
            for n in names:
                self.rows.append((n, n, pygame.Rect(16, y, 268, ROW - 2)))
                y += ROW
            y += 6
        self.volume = conf.num("SOUND_VOLUME")
        self.playing: dict[str, subprocess.Popen] = {}
        self.n = 0

        # mic
        self.levels: deque[float] = deque(maxlen=HISTORY)
        self.min_rms, self.factor = conf.num("MIN_RMS"), conf.num("NOISE_FACTOR")
        self.suppressed = conf.yes("MIC_NOISE_SUPPRESSION") and pw_node(EC_MIC) is not None
        try:
            self.mic: Mic | None = Mic(conf, lambda data: self.levels.append(rms(data)))
        except Exception as e:  # no pw-record
            log.error("no mic: %s", e)
            self.mic = None

        # voice
        self.speaker = Speaker(conf)
        self.text = ""
        self.status = ""
        self.busy = False  # synthesizing or speaking
        self.voice_levels: deque[float] = deque(maxlen=200)
        self.field = pygame.Rect(316, 400, 628, 40)
        self.speak = Button(pygame.Rect(316, 452, 120, 36), "Speak")
        self.stop_btn = Button(pygame.Rect(448, 452, 120, 36), "Stop")
        pygame.key.start_text_input()
        pygame.key.set_repeat(400, 35)
        self.running = True

    # --- actions ---------------------------------------------------------------------------------

    def play_sound(self, name: str) -> None:
        log.info("sound: %s", name)
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.n = (self.n + 1) % 8
        path = RUNTIME / f"dev-sound-{self.n}.wav"
        write_wav(path, make(name), self.volume)
        self.playing[name] = pw_play(path)

    def say(self) -> None:
        text = self.text.strip()
        if not text or self.busy:
            return
        self.busy, self.status = True, "synthesizing…"
        self.voice_levels.clear()
        threading.Thread(target=self._say, args=(text,), name="say", daemon=True).start()

    def _say(self, text: str) -> None:
        try:
            t = time.monotonic()
            wav = self.speaker.synth(text)
            if wav is None:
                self.status = "synthesis failed (see the log)"
                return
            self.status = f"speaking (synthesized in {time.monotonic() - t:.1f} s)"
            self.speaker.play(wav)
            self.status = "done"
        except Exception as e:
            log.exception("voice test failed")
            self.status = f"error: {e}"
        finally:
            self.busy = False

    def stop(self) -> None:
        self.speaker.stop()
        for p in self.playing.values():
            p.terminate()

    # --- loop ------------------------------------------------------------------------------------

    def run(self) -> None:
        clock = pygame.time.Clock()
        while self.running:
            clock.tick(30)
            for ev in pygame.event.get():
                self.event(ev)
            self.playing = {n: p for n, p in self.playing.items() if p.poll() is None}
            if self.speaker.proc:
                self.voice_levels.append(self.speaker.level)
            self.draw()
            pygame.display.flip()
        self.stop()
        if self.mic:
            self.mic.stop()

    def event(self, ev) -> None:
        if ev.type == pygame.QUIT:
            self.running = False
        elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
            for _, name, rect in self.rows:
                if name and rect.collidepoint(ev.pos):
                    self.play_sound(name)
            if self.speak.rect.collidepoint(ev.pos):
                self.say()
            elif self.stop_btn.rect.collidepoint(ev.pos):
                self.stop()
        elif ev.type == pygame.TEXTINPUT:
            self.text += ev.text
        elif ev.type == pygame.KEYDOWN:
            ctrl = ev.mod & pygame.KMOD_CTRL
            if ev.key == pygame.K_q and ctrl:
                self.running = False
            elif ev.key == pygame.K_ESCAPE:
                if self.busy or self.playing:
                    self.stop()
                else:
                    self.running = False
            elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.say()
            elif ev.key == pygame.K_BACKSPACE:
                # Ctrl+Backspace deletes a word
                self.text = self.text.rstrip()[:self.text.rstrip().rfind(" ") + 1] if ctrl else self.text[:-1]
            elif ev.key == pygame.K_u and ctrl:
                self.text = ""
            elif ev.key == pygame.K_v and ctrl:
                self.paste()

    def paste(self) -> None:
        try:
            clip = pygame.scrap.get_text() if pygame.scrap.get_init() else ""
        except Exception:
            clip = ""
        self.text += (clip or "").replace("\n", " ")

    # --- drawing ---------------------------------------------------------------------------------

    def label(self, text: str, pos, font=None, color=TEXT) -> None:
        self.screen.blit((font or self.font).render(text, True, color), pos)

    def draw(self) -> None:
        s, mouse = self.screen, pygame.mouse.get_pos()
        s.fill(BG)
        self.draw_sounds(mouse)
        self.draw_mic()
        self.draw_voice(mouse)

    def draw_sounds(self, mouse) -> None:
        s = self.screen
        pygame.draw.rect(s, PANEL, (8, 8, 284, SIZE[1] - 16), border_radius=8)
        self.label("Sounds", (16, 18), self.big, COLOR)
        self.label("click to play", (110, 24), self.small, DIM)
        for text, name, rect in self.rows:
            if name is None:
                self.label(text.upper(), (rect.x, rect.y + 6), self.small, DIM)
                continue
            if name in self.playing:
                pygame.draw.rect(s, COLOR, rect, border_radius=4)
                color = BG
            else:
                if rect.collidepoint(mouse):
                    pygame.draw.rect(s, LINE, rect, border_radius=4)
                color = TEXT
            if name in self.playing:
                x, y = rect.x + 8, rect.centery
                pygame.draw.polygon(s, color, [(x, y - 5), (x, y + 5), (x + 8, y)])
            self.label(text, (rect.x + 20, rect.y + 4), color=color)

    def draw_mic(self) -> None:
        s = self.screen
        pygame.draw.rect(s, PANEL, (300, 8, SIZE[0] - 308, 336), border_radius=8)
        self.label("Microphone", (316, 18), self.big, COLOR)
        if not self.mic:
            self.label("no mic (pw-record missing?)", (316, 60), color=WARN)
            return
        name = self.mic.name or "default mic"
        self.label(name + ("  ·  noise suppressed" if self.suppressed else ""), (316, 48), self.small, DIM)

        levels = list(self.levels)
        level = levels[-1] if levels else 0.0
        recent = sorted(levels[-50:])
        noise = recent[len(recent) // 5] if recent else 0.0
        threshold = max(self.min_rms, noise * self.factor)
        speech = level > threshold

        # big bar
        bar = pygame.Rect(316, 72, SIZE[0] - 340, 28)
        pygame.draw.rect(s, BG, bar, border_radius=4)
        fill = bar.copy()
        fill.width = round(bar.width * _meter(level))
        pygame.draw.rect(s, SPEECH if speech else COLOR, fill, border_radius=4)
        for value, color in ((noise, DIM), (threshold, SPEECH)):
            x = bar.x + round(bar.width * _meter(value))
            pygame.draw.line(s, color, (x, bar.y - 4), (x, bar.bottom + 4), 2)
        self.label(f"level {level:6.0f}", (316, 108))
        self.label(f"noise {noise:5.0f}", (476, 108), color=DIM)
        self.label(f"threshold {threshold:5.0f}", (616, 108), color=SPEECH)
        if speech:
            self.label("SPEECH", (SIZE[0] - 100, 108), color=SPEECH)

        # last 10 s
        graph = pygame.Rect(316, 140, SIZE[0] - 340, 180)
        pygame.draw.rect(s, BG, graph, border_radius=4)
        w = graph.width / HISTORY
        recent = []
        for i, v in enumerate(levels):
            recent = (recent + [v])[-50:]
            thr = max(self.min_rms, sorted(recent)[len(recent) // 5] * self.factor)
            h = round(graph.height * _meter(v))
            x = graph.x + round(i * w)
            pygame.draw.rect(s, SPEECH if v > thr else COLOR, (x, graph.bottom - h, max(1, round(w) - 1), h))
        y = graph.bottom - round(graph.height * _meter(threshold))
        pygame.draw.line(s, SPEECH, (graph.x, y), (graph.right, y), 1)
        y = graph.bottom - round(graph.height * _meter(noise))
        pygame.draw.line(s, DIM, (graph.x, y), (graph.right, y), 1)
        self.label("last 10 s", (graph.x + 6, graph.y + 4), self.small, DIM)

    def draw_voice(self, mouse) -> None:
        s, c = self.screen, self.conf
        pygame.draw.rect(s, PANEL, (300, 352, SIZE[0] - 308, SIZE[1] - 360), border_radius=8)
        self.label("Voice", (316, 362), self.big, COLOR)
        self.label(f"{c['VOICE']}  ·  effect {c['EFFECT']}  ·  speed {c['SPEED']}  ·  volume {c['VOLUME']}",
                   (390, 368), self.small, DIM)

        # text field, showing the end of the text when it's too long
        pygame.draw.rect(s, BG, self.field, border_radius=6)
        pygame.draw.rect(s, COLOR, self.field, 1, border_radius=6)
        shown = self.text
        while shown and self.font.size(shown)[0] > self.field.width - 24:
            shown = shown[1:]
        if self.text:
            self.label(shown, (self.field.x + 10, self.field.y + 12))
        else:
            self.label("type a message, Enter to speak", (self.field.x + 10, self.field.y + 12), color=DIM)
        if int(time.monotonic() * 2) % 2:
            x = self.field.x + 10 + (self.font.size(shown)[0] if self.text else 0)
            pygame.draw.line(s, TEXT, (x, self.field.y + 9), (x, self.field.bottom - 9), 2)

        self.speak.draw(s, self.font, mouse, enabled=bool(self.text.strip()) and not self.busy)
        self.stop_btn.draw(s, self.font, mouse, enabled=self.busy)
        self.label(self.status, (584, 462), self.small, WARN if self.status.startswith(("error", "synth")) and
                   "…" not in self.status else DIM)

        # loudness while it speaks (what the eyes follow)
        self.label("loudness (drives the speaking eyes)", (316, 498), self.small, DIM)
        env = pygame.Rect(316, 514, SIZE[0] - 340, SIZE[1] - 540)
        pygame.draw.rect(s, BG, env, border_radius=4)
        w = env.width / self.voice_levels.maxlen
        for i, v in enumerate(self.voice_levels):
            h = round(env.height * v)
            pygame.draw.rect(s, COLOR, (env.x + round(i * w), env.bottom - h, max(1, round(w)), h))
        self.label("Ctrl+U clear · Ctrl+V paste · Esc stop / quit", (SIZE[0] - 300, SIZE[1] - 20),
                   self.small, DIM)


def run(conf: Conf) -> None:
    pygame.display.init()
    pygame.font.init()
    tool = DevTool(conf)
    try:
        pygame.scrap.init()
    except Exception:  # no clipboard on this display: no paste
        pass
    try:
        tool.run()
    except KeyboardInterrupt:
        tool.stop()
        if tool.mic:
            tool.mic.stop()
    pygame.quit()
