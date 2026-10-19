"""Hektor: run the face.

Say "hey hector" (or press k / Enter) and ask something; Hektor answers out loud.

Keys:  k/Enter talk: start listening, send now, cancel the question, stop talking
       r new conversation
       f add a face: Hektor asks your name, checks it, and takes your picture (face recognition)
       (with --kiosk, as the robot: only Ctrl+Q quits)
Dev:   1 idle  2 sleeping  3 listening  4 thinking  5 speaking (fake voice)
       h happy  a sad  s surprised  c confused  x angry  (with their sound)
       n an idle noise  b blink  space glance  g glow  l scanlines
       v camera preview  F11 full screen  q/Esc quit
Camera: the eyes follow your face; with nobody around Hektor falls asleep, and wakes when you show up
       (or on the wake word, or a key). Mouse: hold a button and the eyes follow the pointer instead.

Settings: hektor.conf, overridden by ~/.config/hektor/hektor.conf.
Developer tools (dev console, monitor, mic and wake word tests, the noises): dev/run.sh in the repo.
"""
import argparse
import io
import logging
import logging.handlers
import os
import random
import time

os.environ.setdefault("SDL_VIDEODRIVER", "wayland,x11")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pygame  # noqa: E402

from . import config, events, history, share  # noqa: E402
from .eyes import EMOTIONS, Eyes  # noqa: E402
from .sounds import IDLE, Sounds  # noqa: E402
from .stripe import Stripe  # noqa: E402

log = logging.getLogger("hektor")

STATE_KEYS = {pygame.K_1: "idle", pygame.K_2: "sleeping", pygame.K_3: "listening",
              pygame.K_4: "thinking", pygame.K_5: "speaking"}
EMOTION_KEYS = {pygame.K_h: "happy", pygame.K_a: "sad", pygame.K_s: "surprised",
                pygame.K_c: "confused", pygame.K_x: "angry"}


class FakeVoice:
    """A speech-like loudness, syllables and pauses, to try the speaking eyes without audio."""

    def __init__(self) -> None:
        self.level, self.next = 0.0, 0.0

    def update(self, now: float) -> float:
        if now >= self.next:
            pause = random.random() < 0.15
            self.level = 0.0 if pause else random.uniform(0.3, 1.0)
            self.next = now + (random.uniform(0.25, 0.5) if pause else random.uniform(0.08, 0.2))
        return self.level


def setup_logging(debug: bool) -> None:
    """To the terminal, and to ~/.local/state/hektor/hektor.log (kept small, rotated)."""
    config.LOG.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s", "%H:%M:%S")
    to_file = logging.handlers.RotatingFileHandler(config.LOG, maxBytes=2_000_000, backupCount=3)
    to_file.setFormatter(logging.Formatter("%(asctime)s %(levelname).1s %(name)s: %(message)s"))
    to_term = logging.StreamHandler()
    to_term.setFormatter(fmt)
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO, handlers=[to_term, to_file])
    log.info("hektor starting, log in %s%s", config.LOG, " (debug)" if debug else "")


def start_vision(args, conf):
    if args.no_camera or not conf.yes("CAMERA"):
        log.info("camera off")
        return None
    from .vision import Vision
    try:
        return Vision(conf)
    except Exception as e:  # no OpenCV, no model: still show the face
        log.error("no camera: %s", e)
        return None


def start_voice(args, conf, sounds):
    if args.no_voice:
        return None
    from .voice import Voice
    try:
        return Voice(conf, sounds, wake=not args.no_wake)
    except Exception as e:  # missing models etc.: still show the face
        log.error("no voice: %s", e)
        return None


def main() -> None:
    parser = argparse.ArgumentParser(prog="hektor", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--window", action="store_true", help="run in a window instead of full screen")
    parser.add_argument("--kiosk", action="store_true",
                        help="as the robot (the Hektor session): only Ctrl+Q quits, the mouse pointer hidden")
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--resolution", type=int, default=360,
                        help="height the face is drawn at before scaling to the screen (default 360)")
    parser.add_argument("--scanlines", action="store_true", help="start with scanlines on")
    parser.add_argument("--no-glow", action="store_true", help="start with the glow off")
    parser.add_argument("--no-voice", action="store_true", help="just the eyes: no mic, no talking")
    parser.add_argument("--no-camera", action="store_true", help="don't use the camera")
    parser.add_argument("--no-wake", action="store_true", help="don't listen for the wake word, use the talk key")
    parser.add_argument("--say", metavar="TEXT", help="say TEXT with the current voice settings and exit")
    parser.add_argument("--quiet", action="store_true", help="no noises (the voice still talks)")
    parser.add_argument("--debug", action="store_true",
                        help="log more: what the wake word spotter hears, mic levels, every sound")
    args = parser.parse_args()
    setup_logging(args.debug)

    conf = config.load()
    log.info("config: %s", ", ".join(str(f) for f in config.FILES if f.exists()))
    if args.say:
        from .tts import Speaker
        Speaker(conf).say(args.say)
        return

    pygame.display.init()
    dw, dh = pygame.display.get_desktop_sizes()[0]
    aspect = dw / dh if not args.window else 16 / 10
    size = (round(args.resolution * aspect), args.resolution)
    flags = pygame.SCALED | (pygame.RESIZABLE if args.window else pygame.FULLSCREEN)
    screen = pygame.display.set_mode(size, flags, vsync=1)
    log.info("display %s (%s), face drawn at %dx%d, %s", pygame.display.get_driver(), f"{dw}x{dh}",
             *size, "window" if args.window else "full screen")
    pygame.display.set_caption("Hektor")
    pygame.mouse.set_visible(args.window)

    events.start()
    sounds = Sounds(conf, enabled=not args.quiet)
    app = App(args, conf, screen, size, sounds, start_voice(args, conf, sounds), start_vision(args, conf))
    events.emit("camera", on=app.vision is not None)
    events.emit("recognition", on=conf.yes("FACE_RECOGNITION"))
    events.emit("model", name=conf.get("MODEL", ""))
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    log.info("quitting")
    events.emit("stop")
    app.stop()
    pygame.quit()


class App:
    """The main loop: the face, driven by the voice, the camera, the keys and the mouse."""

    def __init__(self, args, conf, screen, size, sounds, voice, vision) -> None:
        self.args, self.screen, self.size = args, screen, size
        self.eyes, self.fake, self.sounds, self.voice, self.vision = Eyes(), FakeVoice(), sounds, voice, vision
        self.glow, self.scanlines = not args.no_glow, args.scanlines
        self.stripe = Stripe(conf) if conf.yes("VOICE_STRIPE") else None
        self.sleep_after = conf.num("SLEEP_AFTER")
        self.greet_after = conf.num("GREET_AFTER")
        self.voice_state = self.eyes_state = "idle"
        self.active = time.monotonic()  # last time someone was there: a face, talking, a key
        self.had_face = False
        self.face_gone = time.monotonic()
        self.next_share, self.shared = 0.0, False
        self.conf_stamp, self.next_conf_check = config.stamp(), 0.0
        self.recognize = conf.yes("FACE_RECOGNITION")
        self.model = conf.get("MODEL", "")
        self.person: str | None = None     # who the camera says is there
        self.look_to_talk, self.look_time = conf.yes("LOOK_TO_TALK"), conf.num("LOOK_TIME")
        self.look_since: float | None = None   # since when someone known has been looking at it
        self.looked_away = True            # they looked away since the last time it listened to them
        self.last_seen: dict[str, float] = {}  # name: when they were last there
        if voice:
            voice.vision = vision
        self.running = True

    def stop(self) -> None:
        share.remove("screen")
        if self.voice:
            self.voice.stop()
        if self.vision:
            self.vision.stop()

    def run(self) -> None:
        clock = pygame.time.Clock()
        while self.running:
            dt = clock.tick(self.args.fps) / 1000
            for ev in pygame.event.get():
                self.event(ev)
            self.follow_voice()
            self.follow_camera()
            self.react()
            self.eyes.update(dt)
            self.eyes.draw(self.screen, glow=self.glow, scanlines=self.scanlines)
            if self.stripe:
                self.stripe.update(dt, self.stripe_state(), self.stripe_level())
                self.stripe.draw(self.screen)
            self.draw_preview()
            self.share_screen()
            self.watch_config()
            pygame.display.flip()

    def watch_config(self) -> None:
        """When the settings change on disk (the monitor app's buttons set CAMERA, FACE_RECOGNITION and
        MODEL in hektor's own hektor.conf), apply what can change while running: the camera on or off,
        face recognition, and the Claude model."""
        now = time.monotonic()
        if now < self.next_conf_check:
            return
        self.next_conf_check = now + 1
        if history.CLEAR_REQUEST.exists():  # the monitor app's Delete
            log.info("deleting the conversation (asked by the monitor app)")
            if self.voice:
                self.voice.reset()
            history.clear()
            history.CLEAR_REQUEST.unlink(missing_ok=True)
            events.emit("cleared")
        stamp = config.stamp()
        if stamp == self.conf_stamp:
            return
        self.conf_stamp = stamp
        conf = config.load()
        want = conf.yes("CAMERA") and not self.args.no_camera
        if want != (self.vision is not None):
            log.info("settings changed: camera %s", "on" if want else "off")
            if want:
                self.vision = start_vision(self.args, conf)
            else:
                self.vision.stop()
                self.vision = None
            if self.voice:
                self.voice.vision = self.vision
            events.emit("camera", on=self.vision is not None)
        recognize = conf.yes("FACE_RECOGNITION")
        if recognize != self.recognize:
            log.info("settings changed: face recognition %s", "on" if recognize else "off")
            if self.vision:
                self.vision.set_recognition(recognize, conf)
            events.emit("recognition", on=recognize)
        self.recognize = recognize
        model = conf.get("MODEL", "")
        if model != self.model:
            log.info("settings changed: Claude model %s", model or "default")
            if self.voice:
                self.voice.brain.conf["MODEL"] = model  # from the next question on, same conversation
            events.emit("model", name=model)
        self.model = model

    def event(self, ev) -> None:
        eyes, voice, sounds = self.eyes, self.voice, self.sounds
        if ev.type == pygame.QUIT:  # also what SDL makes of SIGTERM, e.g. systemctl restart
            self.running = False
            return
        if ev.type != pygame.KEYDOWN:
            return
        self.active = time.monotonic()
        key = ev.key
        if eyes.state == "sleeping" and key not in STATE_KEYS:
            eyes.set_state("idle")  # any key wakes it up
        if self.args.kiosk:
            if key == pygame.K_q and ev.mod & pygame.KMOD_CTRL:
                self.running = False
            elif key in (pygame.K_q, pygame.K_ESCAPE, pygame.K_F11):
                return  # no quitting or leaving full screen by accident
        if key in (pygame.K_q, pygame.K_ESCAPE):
            self.running = False
        elif key in (pygame.K_k, pygame.K_RETURN) and voice:
            voice.talk()
        elif key == pygame.K_r and voice:
            voice.reset()
        elif key in STATE_KEYS:
            log.debug("key: state %s", STATE_KEYS[key])
            eyes.set_state(STATE_KEYS[key])
        elif key in EMOTION_KEYS:
            eyes.emote(EMOTION_KEYS[key])
            sounds.play(EMOTION_KEYS[key])
        elif key == pygame.K_n:
            sounds.play(random.choice(IDLE))
            eyes.glance()
        elif key == pygame.K_b:
            eyes.blink()
        elif key == pygame.K_SPACE:
            eyes.glance()
        elif key == pygame.K_g:
            self.glow = not self.glow
        elif key == pygame.K_l:
            self.scanlines = not self.scanlines
        elif key == pygame.K_v and self.vision:
            self.vision.want_preview = not self.vision.want_preview
        elif key == pygame.K_f and voice:
            voice.add_face()
        elif key == pygame.K_F11:
            pygame.display.toggle_fullscreen()

    def follow_voice(self) -> None:
        voice, eyes = self.voice, self.eyes
        if voice:
            # the voice drives the state when it changes; the dev keys win in between
            if voice.state != self.voice_state:
                self.voice_state = voice.state
                eyes.set_state(voice.state)
            if voice.state != "idle":
                self.active = time.monotonic()
            if voice.mood:
                if voice.mood in EMOTIONS:
                    log.debug("eyes: %s", voice.mood)
                    events.emit("mood", mood=voice.mood)
                    eyes.emote(voice.mood, hold=4)
                voice.mood = None
        if voice and voice.state == "speaking":
            eyes.level = voice.level
        else:
            eyes.level = self.fake.update(eyes.time) if eyes.state == "speaking" else 0.0

    def stripe_state(self) -> str:
        """The eyes' state, with thinking split into parsing (whisper) and generating (Claude, the voice)."""
        state = self.eyes.state
        if state == "thinking" and self.voice and self.voice.step:
            return self.voice.step
        return state

    def stripe_level(self) -> float:
        """Your voice while listening, Hektor's while speaking (the fake one with the dev keys)."""
        if self.eyes.state == "listening":
            return self.voice.heard_level if self.voice else self.fake.update(self.eyes.time) * 0.8
        return self.eyes.level if self.eyes.state == "speaking" else 0.0

    def follow_camera(self) -> None:
        """Look at the mouse while a button is held, else at the face the camera sees, else around."""
        eyes, now = self.eyes, time.monotonic()
        face = self.vision.face if self.vision else None
        if any(pygame.mouse.get_pressed()):
            mx, my = pygame.mouse.get_pos()
            eyes.look_at((mx / self.size[0] * 2 - 1, my / self.size[1] * 2 - 1))
        else:
            eyes.look_at(face)
        self.follow_person(now)
        if face is None:
            if self.had_face:
                self.had_face, self.face_gone = False, now
                events.emit("face", seen=False)
            return
        self.active = now
        if self.had_face:
            return
        # someone showed up
        self.had_face = True
        events.emit("face", seen=True)
        if eyes.state == "sleeping":
            log.info("someone's here: waking up")
            eyes.set_state("idle")
        elif eyes.state == "idle" and now - self.face_gone > self.greet_after:
            log.info("someone's back: hello")
            eyes.emote("happy", hold=1.5)
            self.sounds.play("happy")

    def follow_person(self, now: float) -> None:
        """Who's there, from face recognition: tell the monitor app, and say hello to someone known who's
        been away for a while (GREET_AFTER)."""
        person = self.vision.person if self.vision else None
        if person != self.person:
            self.person = person
            events.emit("person", name=person)
            away = now - self.last_seen.get(person, -1e9) if person else 0
            if person and away > self.greet_after and self.voice:  # (seeing them wakes it up too)
                log.info("%s is back: hello", person)
                self.voice.greet(f"Hi {person}!")
        if person:
            self.last_seen[person] = now
        self.follow_look(person, now)

    def follow_look(self, person: str | None, now: float) -> None:
        """Someone it knows looking at it for LOOK_TIME starts listening, no wake word needed; again only
        after they've looked away (else it would listen over and over while they sit in front of it)."""
        if not self.look_to_talk or not self.voice:
            return
        if not person or not self.vision.facing:
            self.looked_away, self.look_since = True, None
            return
        if self.look_since is None:
            self.look_since = now
        if self.voice.greeting:  # "Hi Anna!": looking meanwhile counts, it listens right after
            return
        if self.voice.state != "idle":  # talking already: looking doesn't start anything new
            self.looked_away, self.look_since = False, None
            return
        if self.looked_away and now - self.look_since >= self.look_time:
            self.looked_away = False
            self.voice.listen_when_seen(person)

    def react(self) -> None:
        eyes, now = self.eyes, time.monotonic()
        # nobody around for a while: fall asleep (a face, the wake word or a key wakes it)
        if (self.sleep_after > 0 and eyes.state == "idle" and now - self.active > self.sleep_after
                and (not self.voice or self.voice.state == "idle")):
            log.info("nobody around for %.0f s: going to sleep", now - self.active)
            eyes.set_state("sleeping")
        # falling asleep and waking up have their sounds
        if eyes.state != self.eyes_state:
            log.info("eyes: %s -> %s", self.eyes_state, eyes.state)
            events.emit("state", state=eyes.state)
            if eyes.state == "sleeping":
                self.sounds.play("yawn")
            elif self.eyes_state == "sleeping":
                self.sounds.play("wakeup")
            self.eyes_state = eyes.state
        # now and then a little noise while idle, with a look around
        if noise := self.sounds.update(eyes.state):
            if noise != "snore":
                eyes.glance()
                if noise in ("bidoo", "trill") and random.random() < 0.4:
                    eyes.emote("happy", hold=0.8)

    def share_screen(self) -> None:
        """What's on the screen, at half size a few times a second, while the monitor watches."""
        now = time.monotonic()
        if now < self.next_share:
            return
        self.next_share = now + 0.25
        if not share.watched("screen"):
            if self.shared:
                share.remove("screen")
                self.shared = False
            return
        small = pygame.transform.smoothscale(self.screen, (self.size[0] // 2, self.size[1] // 2))
        buf = io.BytesIO()
        pygame.image.save(small, buf, "screen.jpg")
        share.write("screen", buf.getvalue())
        self.shared = True

    def draw_preview(self) -> None:
        """The camera picture with the faces boxed, in a corner (key v)."""
        frame = self.vision.preview if self.vision else None
        if frame is None:
            return
        h, w = frame.shape[:2]
        img = pygame.image.frombuffer(frame.tobytes(), (w, h), "RGB")
        ph = self.size[1] // 3
        img = pygame.transform.smoothscale(img, (ph * w // h, ph))
        self.screen.blit(img, (self.size[0] - img.get_width() - 8, self.size[1] - ph - 8))


if __name__ == "__main__":
    main()
