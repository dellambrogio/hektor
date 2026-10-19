# Hektor

A desk robot face: animated eyes that look at you through the camera, listen through the
microphone and answer out loud, with Claude as the brain. It's meant to run full screen on a spare
laptop with Ubuntu (hektor), and is developed on another Linux machine (monitor).

Status: runs on hektor; what is still to do is in [plan.md](../plan.md). The overview of the repo,
deploying and troubleshooting are in the [main README](../README.md).

## Run

Needs Python 3.11+, a Wayland or X11 desktop, PipeWire, a webcam, and for the voice: `whisper-cli` (whisper.cpp),
`ffmpeg`, `espeak-ng` and Claude Code (`claude`, logged in). Once, fetch the speech and face models (~250 MB
into `~/.local/share/hektor`):

```
./fetch-models.sh
./audio-setup.sh     # optional, recommended: noise suppression + echo cancellation (see below)
```

Then:

```
./run.sh --window     # in a window
./run.sh              # full screen
./run.sh --no-voice   # just the eyes
./run.sh --no-camera  # don't use the camera
./run.sh --say "Hello, I am Hektor."   # try the voice settings
./run.sh --quiet      # no noises
./run.sh --kiosk      # as the robot runs: full screen, only Ctrl+Q quits (q, Esc and F11 do nothing)
```

`run.sh` creates `.venv` and installs `requirements.txt` on the first run.
`./run.sh --help` lists all the options. Logs go to the terminal and to `~/.local/state/hektor/hektor.log`
(rotated at 2 MB). `--debug` logs more: what the wake word spotter makes of what it hears, the mic
level every 10 s, every sound.

The developer tools are in `dev/` at the repo root (`../dev/run.sh -h`): `monitor` (Hektor on hektor
and the worker), `console` (click to play noises, mic meter, type text to hear the voice), `mic-test`
(live mic level vs the speech threshold), `wake-test` (just the wake word spotter, live, next to a free
recognizer's guess of what you said) and `sound NAME` (`all` for every noise).

### In VS Code

Open the repo folder (one level up, where `.vscode/` is), install the Python extension, then Run and Debug
(`Ctrl+Shift+D`) → **Hektor (window)**, **Hektor (full screen)**, **Hektor (eyes only)**, **Hektor (dev
console)** or **Hektor (monitor app)** → `F5`. Breakpoints work. If the interpreter isn't picked up:
`Ctrl+Shift+P` → "Python: Select Interpreter" → `./app/.venv/bin/python`.

### Keys

| Key                   | Does                                            |
|-----------------------|-------------------------------------------------|
| `k` / `Enter`         | talk: start listening, send now, cancel the question, stop talking |
| `r`                   | new conversation                                |
| `1` … `5`             | state: idle, sleeping, listening, thinking, speaking (fake voice) |
| `h` `a` `s` `c` `x`   | happy, sad, surprised, confused, angry (2.5 s), with their sound |
| `n`                   | an idle noise                                   |
| `b` / `space`         | blink / look somewhere else                     |
| `g` / `l`             | glow / scanlines on and off                     |
| `v`                   | camera preview with the faces boxed, on and off |
| `f`                   | add a face: Hektor asks your name, checks it, takes your picture |
| `F11`                 | full screen on and off                          |
| `q` / `Esc`           | quit (with `--kiosk`: only `Ctrl+Q`)            |
| hold a mouse button   | the eyes follow the pointer instead of your face |

## Layout

```
hektor/
  __main__.py   window, main loop, keys
  eyes.py       eye model, expressions, animation, drawing
  voice.py      conversation loop: idle → listening → thinking → speaking
  audio_in.py   mic (PipeWire), wake word (Vosk), recording until you go quiet
  stt.py        speech to text (whisper.cpp)
  brain.py      Claude through `claude -p`
  tts.py        piper voice, ffmpeg effect, playback, loudness for the eyes
  sounds.py     robot noises: synthesized chirps, idle noises, mood sounds
  vision.py     camera and face detection (OpenCV YuNet)
  faces.py      face recognition (OpenCV SFace): who's there; names and yes / no for adding a face (f)
  config.py     reads hektor.conf
  stripe.py     the voice stripe under the eyes: listening, thinking, speaking
  events.py     what Hektor does, one JSON line per event (~/.local/state/hektor/events.jsonl)
  share.py      the screen and camera pictures for the monitor, only while it's watching
  history.py    deleting what was said, when the monitor app's Delete asks (events, logs, Claude's sessions)
hektor.conf     settings: voice, effect, wake word, mic, Claude prompt
fetch-models.sh downloads the speech models
audio-setup.sh  adds the "Hektor mic" to PipeWire (pipewire/hektor-echo-cancel.conf)
run.sh
requirements.txt
```

hektor's setup is in `../ubuntu/` ([its README](../ubuntu/README.md)).

## How the eyes work

Each eye is a rounded rectangle described by a few numbers (`Eye` in `eyes.py`): offset, width, height,
corner radius, top lid height and angle, curved bottom lid. An expression is a preset of those numbers for
both eyes (`EXPRESSIONS`), and the face tweens from one to the next. On top of that, `Eyes.update()` adds
gaze, blinks, breathing while asleep, jitter while thinking and bouncing with the voice level. The face is
drawn at a low resolution with 2× supersampling and scaled up to the screen by SDL.

To add an expression, add an entry to `EXPRESSIONS` (and to `EMOTIONS` if it's a passing emotion).

## How the voice works

Say "hey hector" (the Vosk model has no "Hektor", it sounds the same) and ask your question in the same
breath or right after. The mic is one PipeWire stream: while idle it goes to the wake word spotter, which
runs offline; while listening it's recorded until you stop talking (below). whisper.cpp turns it into
text, `claude -p` answers (resuming the conversation until 5 minutes of silence), and piper speaks the
reply through an ffmpeg effect while the eyes bounce with its loudness. The model is `MODEL`: Haiku 5.5 by default, the
fastest to start answering (~1.5 s against ~2.5 s for Sonnet); the monitor app's conversation panel
switches it while Hektor runs. Claude starts each reply with a
mood like `[happy]`, which shows on the eyes and isn't spoken. Non-English replies are spoken by espeak-ng.

The conversation stays open: after each answer Hektor listens again (the "bwee-dip" chirp, the stripe
red), so you just go on talking, no "hey hector" in between. Quiet for `FOLLOW_UP` seconds (6) ends it
("pwoo"), and it waits for the wake word again. `FOLLOW_UP=0` turns this off.

Claude can search the web and read (not change) Gmail, Calendar, Drive and Claude Docs; anything else is
refused, since there's nobody to approve it.

A stripe under the eyes, like KITT's scanner, shows the conversation: red and moving with your voice
while it listens (so you can see it hears you), an amber light sweeping back and forth while whisper works
out what you said, a violet one while Claude answers and the voice is made, teal with its own voice while
it speaks (`VOICE_STRIPE`, the colors in `STRIPE_*`, `stripe.py`).

To cut in while Hektor is talking, say "hey hector" again (or press `k`): it stops and listens to the new
question. While it's thinking, only `k` cancels: the wake word spotter took people still talking for the
wake word and kept cancelling their questions. With `INTERRUPT="wake talk"` simply talking over it works too: what you've said
already becomes the start of the question. That needs the echo canceller (below) so Hektor doesn't hear
itself; its voice still leaks through faintly (up to ~300 here), so you have to be louder than
`INTERRUPT_RMS` (600) for 0.4 s. `--debug` logs the loudest level heard during each reply, to tune it.

Settings are in `hektor.conf`; put per-machine changes in `~/.config/hektor/hektor.conf`.

### How it tells talking from noise

The mic hears plenty that isn't you: fans, typing, thumps, Hektor's own chirps. So a 100 ms chunk only
sounds like talking when it's loud (over the background noise × `NOISE_FACTOR`) **and voiced**: vowels
have a pitch, fans, hiss, clicks and thumps don't (`voicing()` in `audio_in.py`, at least `VOICED`, 0.6).
You're talking when 3 of the last 5 chunks do (`SPEECH_RUN`). That decides when speech starts and when it
ends: after `SILENCE_END` (1.5 s) without talking, where "loud" is now measured against your speech
(under `SILENCE_SHARE`, 20%, of its typical level): the room is often louder while you talk than in the
quiet moment the background was measured. The first 0.5 s of a recording, when Hektor's own "I'm
listening" chirp can still be heard, doesn't count, and the chirp is cut from what whisper gets. A wake
word only counts with some voice in the 1.5 s before it: the spotter, which only knows its phrases,
takes thumps for them too (those show as "wake word ignored"). The log has the levels of each recording;
whatever whisper returned without words ("(speaking in foreign language)" and the like) is logged too.

## How the camera works

A thread reads the webcam (the MX Brio if it's there, else the first color camera: the laptop's infrared
face-login camera is skipped) and looks for faces about 8 times a second with OpenCV's YuNet detector.
The eyes look at the biggest face. With nobody around (no face, no talking, no keys) for 5 minutes,
Hektor yawns and falls asleep; a face showing up wakes it, and someone coming back after a minute away
gets a happy hello. If the eyes look away from you instead of at you, set `CAMERA_MIRROR=yes`. Settings
are in the Camera part of `hektor.conf`; `CAMERA=no` turns it off (as `--no-camera`). The picture isn't
stored; it only leaves the machine while a monitor (`dev/run.sh monitor`) is watching, as a small JPEG a few times
a second over SSH (`share.py`), deleted again a few seconds after the monitor closes.

### Knowing who's there

Hektor recognizes people who asked it to. Stand in front of it and press **`f`**:

1. Hektor: "Add a new face. Please tell me your name." You: "Anna" (or "it's Anna", "my name is Anna").
2. Hektor: "Anna. Is that right?" You: "yes". With "no" it asks the name again (up to three times).
3. Hektor: "Okay Anna, look at me. I'm taking your picture." It takes about 12 fingerprints of your face
   while you look at it (move your head a little), a shutter sound, then "Nice to meet you, Anna!".

Saying "hey hector, remember me" (or "remember me, I'm Anna", which skips the name question) does the
same. Silence, or no clear answer, cancels it. From then on:

- it says "Hi Anna!" when you show up after a minute away (or at the first sight after it starts);
- looking at it for a second and a half makes it listen, no "hey hector" needed (`LOOK_TO_TALK`); once
  per look: after a conversation, look away and back to start another. Strangers still say "hey hector";
- each question goes to Claude with a note, "[Anna is in front of you]", so answers can use your name;
- the monitor app shows "Anna" instead of "someone's there".

**"Forget me"** deletes the fingerprints of whoever it recognizes. Adding your face again replaces
yours (in other light, with glasses, ...: do it again where it gets you wrong).

How: OpenCV's SFace turns the biggest face YuNet finds into a fingerprint, 128 numbers, when the face
shows up and every 3 s after; it's someone when it's close enough to one of their fingerprints
(`FACE_MATCH`, cosine similarity). Fine for greeting a few people, not for security: it's weaker in profile,
in the dark, or with a different look than when you enrolled.

Privacy: only people who asked are stored, as fingerprints, not pictures, in
`~/.local/share/hektor/faces.json` on hektor (readable by `hektor` only); unknown faces are never stored.
`FACE_RECOGNITION=no` turns it off. To see who's known, rename or forget someone: the faces panel in the monitor app (pencil, trash can), or
`dev/run.sh faces` on hektor.

## How the noises work

Hektor makes little robot noises: a chirp when it wakes up (wake word or `k`), a blip when it
got your question, a "never mind" when it heard nothing, a sound for the mood of each reply (played with
the expression, just before it talks), a yawn when it falls asleep and a stretch when it wakes, and every
25–90 s while idle a small noise with a look around, to show it's alive. They're synthesized in
`sounds.py` from a few harmonics, pitch slides and vibrato, randomized a little each time, so no two are
the same. Volume, idle timing and snoring are `SOUND_VOLUME`, `IDLE_SOUNDS` and `SLEEP_SOUNDS` in
`hektor.conf`; to change a sound, edit its function in `SOUNDS`.

### Noise suppression

`audio-setup.sh` adds PipeWire's WebRTC echo canceller (the one video calls use) as two extra devices,
"Hektor mic" and "Hektor speaker". The app routes the mic it picks into it, records the cleaned-up
"Hektor mic", and plays its voice to "Hektor speaker" so the mic doesn't hear it. Your default mic and
output don't change. On the development laptop's built-in mic it brought the background noise from ~4500
down to ~160; on hektor it's ~150–650, depending on the fan and the room. `./audio-setup.sh --remove` takes it out again; without it the app uses the plain mic.
