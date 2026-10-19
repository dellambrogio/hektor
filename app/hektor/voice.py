"""The conversation loop: idle → listening → thinking → speaking → idle.

Runs on background threads (the mic, and one worker per question); the face reads `state`, `level`
and `mood` every frame.
"""
import logging
import re
import subprocess
import threading
import time
from collections import deque

from . import events, faces
from .audio_in import Mic, Recorder, WakeWord, rms, voicing
from .brain import ALLOWED_TOOLS, TOOL, Brain
from .config import RUNTIME, Conf
from .sounds import Sounds
from .stt import Server, Worker, transcribe, write_wav
from .tts import Speaker, sentences, speakable

log = logging.getLogger(__name__)
# said while Claude searches or checks something, if it hasn't said anything yet
LOOKUPS = {"WebSearch": "Let me look that up.", "WebFetch": "Let me look that up.",
           **{tool: "Let me check." for tool in ALLOWED_TOOLS if tool.startswith("mcp__")}}
LOOKING = re.compile(r"^(?:ok(?:ay)?,? )?(?:let me|i'll|i will|one moment|just a moment)\b", re.I)  # Claude's own
SOURCES = re.compile(r"(?:^|\n)\W*sources?\W*:", re.I)  # where a list of links starts


class Voice:
    def __init__(self, conf: Conf, sounds: Sounds, wake: bool = True) -> None:
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.conf = conf
        self.sounds = sounds
        self.state = "idle"
        self.mood: str | None = None   # set with each reply, the face takes it and clears it
        self.step: str | None = None   # while thinking: parsing (whisper), then generating (Claude, voice)
        self.brain = Brain(conf)
        self.speaker = Speaker(conf)
        self.vision = None             # the camera (vision.Vision), set by the app: who's there, enrolling
        self.answer: dict | None = None  # while Hektor waits for an answer to its own question (_ask)
        self.listening_for = ""        # why it's listening: wake word, talk key, follow-up, question, seen, ...
        self.greeting = False          # saying hello of its own accord (greet): not a conversation
        # everything that's slow the first time, loaded now rather than on the first question
        self.whisper = Server(conf)
        self.worker = Worker(conf)
        self.speaker.warm_up()
        self.brain.warm_up()
        self.wake = WakeWord(conf) if wake and conf.yes("WAKE") else None
        self.levels: deque[float] = deque(maxlen=50)  # last 5 s of mic loudness while idle
        self.voiced: deque[bool] = deque(maxlen=15)   # last 1.5 s while idle: loud and voiced?
        self.min_voiced = conf.num("VOICED")
        self.recorder: Recorder | None = None
        self.mic_level = 0.0           # loudness of the last chunk while listening
        self.turn = 0                  # bumped to cancel the current question
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.busy = False
        self.stats: list[float] = []   # idle mic levels since the last debug summary
        interrupt = conf.get("INTERRUPT", "").split()
        self.interrupt_wake = "wake" in interrupt and self.wake is not None
        self.interrupt_talk = "talk" in interrupt
        self.interrupt_rms, self.interrupt_run = conf.num("INTERRUPT_RMS"), conf.int("INTERRUPT_RUN")
        self.talked: list[bytes] = []  # loud chunks in a row while Hektor talks: maybe you, cutting in
        self.loudest = 0.0             # loudest mic level while Hektor talked, to tune INTERRUPT_RMS
        self.mic = Mic(conf, self._chunk)
        log.info("voice ready: wake word %s, interrupt %s, voice %s, effect %s, whisper %s, model %s",
                 "on" if self.wake else "off", " ".join(interrupt) or "off", conf["VOICE"], conf["EFFECT"], conf.path("WHISPER_MODEL").name,
                 conf.get("MODEL") or "default")

    @property
    def level(self) -> float:
        return self.speaker.level if self.state == "speaking" else 0.0

    @property
    def heard_level(self) -> float:
        """How loud you are while it listens, 0-1 (0 below the speech threshold), for the voice stripe."""
        rec = self.recorder
        if self.state != "listening" or rec is None:
            return 0.0
        return min(1.0, max(0.0, (self.mic_level - rec.threshold * 0.7) / (rec.threshold * 6))) ** 0.6

    # --- control, from the face's keys ----------------------------------------------------------

    def talk(self) -> None:
        """Like clicking KITT: start listening, send now, cancel the question, or stop talking."""
        with self.lock:
            log.info("talk key while %s", self.state)
            match self.state:
                case "idle":
                    self._listen("talk key")
                case "listening":
                    self._recorded(self.recorder.finish(), "talk key")
                case "thinking":
                    self._cancel()
                    self.state = "idle"
                    log.info("question cancelled")
                    events.emit("cancelled", what="question")
                case "speaking":
                    self._cancel()  # the rest of the answer too
                    self.speaker.stop()
                    self.state = "idle"
                    log.info("stopped talking")
                    events.emit("cancelled", what="talking")

    def reset(self) -> None:
        """Stop everything and start a new conversation."""
        with self.lock:
            self._cancel()
            self.speaker.stop()
            self.recorder = None
            self.answer = None
            self.brain.forget()
            self.state = "idle"
        log.info("new conversation")
        events.emit("reset")

    def say(self, text: str) -> None:
        self.speaker.say(text)

    def add_face(self) -> None:
        """The f key: ask the name of whoever's in front of the camera, check it, and remember their face."""
        with self.lock:
            if self.state != "idle":
                return
            self.turn += 1
            turn, self.state, self.step = self.turn, "thinking", None
        log.info("add a face (f key)")
        threading.Thread(target=self._add_face, args=(turn, None), name="add-face", daemon=True).start()

    def listen_when_seen(self, person: str) -> None:
        """Someone it knows looks at it: listen, as after the wake word, if it's not busy. With no answer
        it goes back to idle quietly."""
        with self.lock:
            if self.state != "idle":
                return
            log.info("%s is looking at me: listening", person)
            events.emit("looked", name=person)
            self._listen("seen", no_speech=self.conf.num("FOLLOW_UP") or self.conf.num("NO_SPEECH"))

    def greet(self, text: str) -> None:
        """Say something short of its own accord (hello to someone it knows), if it's not busy."""
        with self.lock:
            if self.state != "idle":
                return
            self.turn += 1
            turn, self.state, self.step = self.turn, "thinking", None
            self.greeting = True

        def run() -> None:
            try:
                self._speak(turn, text, "happy")
            finally:
                with self.lock:
                    self.greeting = False
                    if turn == self.turn:
                        self.state = "idle"
        threading.Thread(target=run, name="greet", daemon=True).start()

    def stop(self) -> None:
        self.mic.stop()
        self.reset()
        self.whisper.stop()

    # --- mic thread -----------------------------------------------------------------------------

    def _chunk(self, data: bytes) -> None:
        with self.lock:
            state = self.state
            if state == "listening":
                self.mic_level = rms(data)
                if (done := self.recorder.add(data)) is not None:
                    self._recorded(done)
                return
        if state != "idle":
            self.busy = True
            self._interrupt(state, data)
            return
        if self.busy:  # back from a conversation: forget what was half heard, and the voice, before it
            self.voiced.clear()
            if self.wake:
                self.wake.reset()
        self.busy = False
        level = rms(data)
        self.levels.append(level)
        if log.isEnabledFor(logging.DEBUG):
            self.stats.append(level)
            if len(self.stats) >= 100:  # every 10 s
                s = sorted(self.stats)
                log.debug("mic idle 10 s: quiet %.0f, median %.0f, loudest %.0f", s[len(s) // 5], s[len(s) // 2], s[-1])
                self.stats.clear()
        noise = sorted(self.levels)[len(self.levels) // 5]
        self.voiced.append(level > max(self.conf.num("MIN_RMS"), noise * self.conf.num("NOISE_FACTOR"))
                           and voicing(data) >= self.min_voiced)
        if self.wake and self.wake.heard(data):
            if sum(self.voiced) < 2:  # the spotter takes thumps and clicks for its phrases too
                log.info("wake word ignored: no voice in the last 1.5 s (level %.0f)", level)
                events.emit("ignored", level=round(level))
                return
            log.info("wake word (level %.0f)", level)
            events.emit("wake", level=round(level))
            with self.lock:
                if self.state == "idle":
                    self._listen("wake word")

    def _interrupt(self, state: str, data: bytes) -> None:
        """While Hektor talks: the wake word, or you talking over it, stops it and it listens.

        Not while it thinks: people go on talking then, and the wake word spotter, which only knows the
        wake phrases, takes plain speech for them and kept cancelling the question.
        """
        if state != "speaking":
            return
        why = None
        if self.interrupt_wake and self.wake.heard(data):
            why = "wake word"
        if state == "speaking" and self.interrupt_talk and self.mic.cancelled:
            level = rms(data)
            self.loudest = max(self.loudest, level)
            self.talked = self.talked + [data] if level > self.interrupt_rms else []
            if len(self.talked) >= self.interrupt_run and not why:
                why = "talking"
        if not why:
            return
        with self.lock:
            if self.state != state:
                return
            log.info("interrupted while %s (%s)", state, why)
            events.emit("interrupted", state=state, why=why)
            self._cancel()
            self.speaker.stop()
            # talking: what was said already is the start of the question
            self._listen(why, self.talked if why == "talking" else [])
        if self.wake:
            self.wake.reset()

    # --- conversation (called with self.lock held) ----------------------------------------------

    def _listen(self, why: str, start: list[bytes] | None = None, no_speech: float | None = None) -> None:
        """Start recording you (`no_speech`: give up after this long without talking, NO_SPEECH if None)."""
        # background noise from the quiet moments between words, so speech that starts right away
        # isn't mistaken for it
        noise = sorted(self.levels)[len(self.levels) // 5] if self.levels else 0.0
        self.recorder = Recorder(self.conf, noise)
        if no_speech:
            self.recorder.no_speech = no_speech
        if start:
            self.recorder.prefill(start)
        self.talked = []
        self.state, self.listening_for = "listening", why
        log.info("listening (%s): noise %.0f, speech threshold %.0f", why, noise, self.recorder.threshold)
        events.emit("listening", why=why, noise=round(noise), threshold=round(self.recorder.threshold))
        self.sounds.play("wake")

    def _recorded(self, said: bool, why: str = "") -> None:
        rec = self.recorder
        audio, self.recorder = rec.audio(), None
        log.info("recorded %.1f s (%s): %s, loudest %.0f vs threshold %.0f, quiet under %.0f", rec.t,
                 why or ("too long" if rec.t >= rec.max_len else "quiet after speech" if rec.heard else "nothing said"),
                 "speech" if rec.heard else "no speech", rec.loudest, rec.threshold, rec.quiet_level)
        box, self.answer = self.answer, None
        if not said:
            events.emit("nothing", seconds=round(rec.t, 1), why=self.listening_for)
            self.state = "idle"
            if self.listening_for != "seen":  # they only looked: no "never mind" noise
                self.sounds.play("dismiss")
            if box:  # the question Hektor asked got no answer
                box["turn"] = self.turn
                box["event"].set()
            return
        self.state, self.step = "thinking", "parsing"
        self.sounds.play("ack")
        self.turn += 1
        threading.Thread(target=self._think, args=(self.turn, audio, box), name="think", daemon=True).start()

    def _cancel(self) -> None:
        self.turn += 1
        if self.proc:
            self.proc.kill()

    # --- worker thread --------------------------------------------------------------------------

    def _start(self, turn: int, cmd: list[str], **kw) -> subprocess.Popen | None:
        """Start a command for question `turn`, its output piped (cancelling kills it); None if the
        question was cancelled already."""
        with self.lock:
            if turn != self.turn:
                return None
            kw.setdefault("stderr", subprocess.PIPE)
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True, **kw)
            return self.proc

    def _run(self, turn: int, cmd: list[str], **kw) -> str | None:
        """Run a command for question `turn`; None if it failed or the question was cancelled."""
        if not (proc := self._start(turn, cmd, **kw)):
            return None
        out, err = proc.communicate()
        rc, self.proc = proc.returncode, None
        if turn != self.turn:
            return None
        if rc:
            log.error("%s failed (%s): %s", cmd[0], rc, err.strip()[-500:])
            return None
        return out

    def _think(self, turn: int, audio: bytes, box: dict | None = None) -> None:
        """What was said: whisper, then Claude's answer; or, when `box`, the answer to Hektor's own
        question, handed back to the dialog that asked it (_ask)."""
        def run(cmd: list[str], **kw) -> str | None:
            return self._run(turn, cmd, **kw)

        try:
            wav = RUNTIME / "heard.wav"
            write_wav(wav, audio)
            t0 = time.monotonic()
            heard = transcribe(self.conf, wav, run, self.whisper, self.worker)
            stt_s = round(time.monotonic() - t0, 1)
            if turn != self.turn:
                return
            if box:  # the dialog goes on with this turn, and says when it's done
                box["text"], box["turn"] = heard[0] if heard else None, turn
                if heard:
                    log.info("whisper took %.1f s (%s)", stt_s, heard[2])
                    events.emit("heard", text=heard[0], lang=heard[1], stt=stt_s, stt_on=heard[2])
                box["event"].set()
                turn = -1  # not ours any more: leave the state alone
                return
            if not heard:
                log.info("whisper took %.1f s", stt_s)
                log.info("didn't catch that")
                events.emit("unclear", seconds=round(len(audio) / 32000, 1), stt=stt_s)
                self.mood = "confused"
                self.sounds.play("confused")
                return
            text, lang, where = heard
            log.info("whisper took %.1f s (%s)", stt_s, where)
            log.info("heard (%s): %s", lang, text)
            events.emit("heard", text=text, lang=lang, stt=stt_s, stt_on=where)
            self.step = "generating"
            if wish := faces.intent(text):
                if wish[0] == "remember":
                    self._add_face(turn, wish[1])
                    turn = -1  # _add_face left the state as it should be
                else:
                    self._forget_face(turn)
                return
            self._answer(turn, text, lang, t0)
        except Exception as e:
            log.exception("conversation failed")
            events.emit("error", text=f"conversation failed: {e}")
        finally:
            with self.lock:
                if turn == self.turn and self.state != "listening":  # listening: the follow-up
                    self.state = "idle"

    def _speak(self, turn: int, text: str, mood: str | None = None) -> bool:
        """Say `text` (not from Claude) and wait until it's said; False when cancelled."""
        wav = self.speaker.synth(text)
        with self.lock:
            if turn != self.turn or not wav:
                return False
            if mood:
                self.mood = mood
            self.state = "speaking"
            self.talked, self.loudest = [], 0.0
        log.info("saying: %s", text)
        self.speaker.play(wav)
        return turn == self.turn

    def _ask(self, dialog: dict, question: str, mood: str | None = None) -> str | None:
        """Say `question`, listen, and return what was answered (None: nothing, or cancelled). The
        answer comes in a new turn: `dialog["turn"]` follows it."""
        if not self._speak(dialog["turn"], question, mood):
            return None
        box = {"event": threading.Event(), "text": None, "turn": None}
        with self.lock:
            if dialog["turn"] != self.turn:
                return None
            self.answer = box
            self._listen("question")
        box["event"].wait(self.conf.num("NO_SPEECH") + self.conf.num("MAX_LEN") + 30)
        with self.lock:
            if self.answer is box:
                self.answer = None
        if box["turn"] is None or box["turn"] != self.turn:
            return None
        dialog["turn"] = box["turn"]
        return box["text"]

    def _add_face(self, turn: int, name: str | None) -> None:
        """Ask the name (unless given), say it back to check it, then take the picture: look at the
        face for a few seconds and keep its fingerprints under that name (faces.py)."""
        dialog = {"turn": turn}
        try:
            vision = self.vision
            if not vision or not vision.faces:
                self._speak(turn, "I can't do that: my camera or my face recognition is off.", "sad")
                return
            for attempt in range(3):
                if not name:
                    heard = self._ask(dialog, "Add a new face. Please tell me your name." if attempt == 0
                                      else "Okay, tell me your name again.", "surprised" if attempt == 0 else None)
                    if heard is None:
                        self._speak(dialog["turn"], "Okay, never mind.")
                        return
                    name = faces.name_from(heard)
                    if not name:
                        continue
                ok = faces.yes_no(self._ask(dialog, f"{name}. Is that right?"))
                if ok:
                    break
                name = None
            if not name or not ok:
                self._speak(dialog["turn"], "Let's try again later.", "sad")
                return
            if not self._speak(dialog["turn"], f"Okay {name}, look at me. I'm taking your picture.", "happy"):
                return
            with self.lock:
                if dialog["turn"] != self.turn:
                    return
                self.state, self.step = "thinking", None
            prints = vision.enroll()
            if dialog["turn"] != self.turn:
                return
            if len(prints) < 4:
                log.info("adding %s: only %d fingerprints", name, len(prints))
                self._speak(dialog["turn"], "I couldn't see your face well enough. Come a bit closer, "
                                            "in good light, and press f again.", "sad")
                return
            self.sounds.play("shutter")
            vision.faces.add(name, prints)
            vision.person = name
            events.emit("enrolled", name=name, samples=len(prints))
            self._speak(dialog["turn"], f"Got it. Nice to meet you, {name}!", "happy")
        finally:
            with self.lock:
                if dialog["turn"] == self.turn:
                    self.state = "idle"

    def _forget_face(self, turn: int) -> None:
        """"Forget me": delete the fingerprints of whoever's there."""
        vision = self.vision
        if not vision or not vision.faces:
            self._speak(turn, "I can't do that: my camera or my face recognition is off.", "sad")
            return
        who = vision.person
        if not who:
            self._speak(turn, "I don't know who you are, so there's nothing to forget.", "confused")
            return
        vision.faces.forget(who)
        events.emit("forgot", name=who)
        vision.person = None
        self._speak(turn, f"Done, {who}. I've forgotten your face.", "sad")

    def _answer(self, turn: int, text: str, lang: str, t0: float) -> None:
        """Ask Claude and say the answer a sentence at a time as it streams in: each sentence is made
        while the one before it plays. `t0`: when the recording ended."""
        t = time.monotonic()
        session = self.brain.session
        mood, spoken, rest, wait_s = "neutral", [], "", None

        def say(sentence: str) -> bool:
            """Speak one sentence after the ones before it; False when the question was cancelled."""
            nonlocal wait_s
            wav = self.speaker.synth(sentence, lang, name=f"reply-{len(spoken) % 2}", pause=bool(spoken))
            if turn != self.turn or not wav:
                return turn == self.turn
            if not spoken:  # the face shows the mood, with its sound, then Hektor talks
                self.mood = mood
                self.sounds.play(mood, wait=True)
                with self.lock:
                    if turn != self.turn:
                        return False
                    self.state = "speaking"
                    self.talked, self.loudest = [], 0.0
                wait_s = round(time.monotonic() - t0, 1)
                log.info("talking %.1f s after the question (claude's first sentence at %.1f s)",
                         wait_s, first_s)
            self.speaker.start(wav)
            spoken.append(sentence)
            return True

        first_s, looking_up, sources = 0.0, False, False
        person = self.vision.person if self.vision else None
        question = f"[{person} is in front of you] {text}" if person else text
        for piece_mood, piece in self.brain.ask(question, lambda cmd, **kw: self._start(turn, cmd, **kw)):
            if turn != self.turn:
                return
            if piece_mood == TOOL:  # a search, the mail...: say so if nothing's been said yet, it takes a while
                if not spoken and not rest.strip() and not looking_up and piece in LOOKUPS:
                    looking_up = True
                    if not say(LOOKUPS[piece]):
                        return
                continue
            if sources:  # the list of links after an answer from the web: not for reading out
                continue
            if m := SOURCES.search(rest + piece):
                piece, sources = (rest + piece)[:m.start()], True
                rest = ""
            if piece_mood:
                mood = piece_mood
                if spoken:
                    self.mood = mood
                rest = rest.rstrip() + " " if rest.strip() else ""
            done, rest = sentences(rest + piece)
            if sources:  # what came before the list is whole
                done, rest = done + [rest], ""
            for sentence in done:
                sentence = speakable(sentence)
                if not sentence or (looking_up and len(spoken) == 1 and LOOKING.match(sentence)):
                    continue  # nothing to say, or Claude's own "let me look that up" right after Hektor's
                if not first_s:
                    first_s = time.monotonic() - t
                if not say(sentence):
                    return
        claude_s = round(time.monotonic() - t, 1)
        log.info("claude took %.1f s", claude_s)
        if turn != self.turn:
            return
        if speakable(rest):
            first_s = first_s or claude_s
            if not say(speakable(rest)):
                return
        if not spoken:
            log.info("no answer")
            events.emit("error", text="no answer from Claude")
            return
        reply = " ".join(spoken)
        log.info("reply [%s]: %s", mood, reply)
        events.emit("reply", text=reply, mood=mood, claude=claude_s, wait=wait_s, new=self.brain.session != session)
        self.speaker.wait()
        if turn != self.turn:
            return
        log.info("done talking")
        if self.interrupt_talk:
            log.debug("loudest mic level while talking: %.0f (INTERRUPT_RMS %.0f)", self.loudest, self.interrupt_rms)
        # the conversation stays open: your turn, without the wake word, for FOLLOW_UP seconds
        follow_up = self.conf.num("FOLLOW_UP")
        if follow_up > 0:
            with self.lock:
                if turn == self.turn:
                    self._listen("follow-up", no_speech=follow_up)
