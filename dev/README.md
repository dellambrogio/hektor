# Developer tools

Tools for working on Hektor, not used by the robot itself. They use the app's code (`app/hektor`) and
its venv (made on the first run). From the repo root:

```
dev/run.sh monitor [HOST]   # watch Hektor on hektor, and the worker here
dev/run.sh console          # try the noises, the mic level and the voice in a window
dev/run.sh mic-test         # the mic level live against the speech threshold, in the terminal
dev/run.sh wake-test        # what the wake word spotter hears, in the terminal
dev/run.sh sound NAME       # play one of Hektor's noises ("all" for each in turn)
dev/run.sh screenshots      # redraw doc/eyes.png and doc/monitor.png (off-screen, demo data)
dev/run.sh faces [forget NAME | rename OLD NEW]  # who Hektor knows by face (on hektor); forget, rename
dev/run.sh -h               # the list; --debug logs more
```

Deploys copy `dev/` to hektor too, so the terminal checks work there: `~/hektor/dev/run.sh mic-test`.
In VS Code: Run and Debug → **Hektor (dev console)** or **Hektor (monitor app)**.

## Monitor

A window that watches Hektor on hektor over SSH (HOST: `user@machine`, default `HEKTOR_HOST` from
`deploy.conf`; `local` watches this computer), and the worker on monitor.

| Panel        | Shows                                                                          |
|--------------|--------------------------------------------------------------------------------|
| top bar      | Hektor's service running or not, since when; what it's doing now (idle, listening, ...), its mood, how long whisper and Claude took and how soon it talked; **Shut down** (click twice) switches hektor off: the app does it from its desktop session, no sudo needed; the power button turns it on again |
| screen       | what hektor's screen shows (the eyes), ~3 times a second                       |
| camera       | what the camera sees, the faces boxed; a button turns Hektor's camera on and off (`CAMERA` in hektor's `~/.config/hektor/hektor.conf`, applied while it runs); `c` shows / hides the picture here |
| faces        | who it sees now (the name, someone, nobody); a button turns face detection (recognition: who it is, `FACE_RECOGNITION`) on and off; who it knows, from hektor's `faces.json` (only the names and how many fingerprints come over), each with a pencil to rename them (type, Enter saves, Esc cancels) and a trash can to delete them (click twice: the second click confirms) |
| conversation | what it heard and answered, with timings and where whisper ran; wake words, misses, interruptions. A menu switches the Claude model Hektor asks (Haiku 5.5, the fastest, by default; `MODEL` on hektor, applied from the next question). **Delete** (click twice) deletes it here and on hektor: what was said in its events and log, Claude's saved conversations there, the last recording; the conversation starts over (`app/hektor/history.py`) |
| worker       | the worker running or not, the tunnel, BUSY while it transcribes, the worker's log; a button (or `w`) starts and stops it |
| system       | CPU (the last minute) and RAM of hektor and monitor, filling the panel |

CPU graphs are the last minute, as a share of the whole machine (all its cores); RAM bars are a share of the machine's RAM.

The pictures are made by the app only while a monitor watches (`app/hektor/share.py`) and deleted a few
seconds after. Drag the gaps between the panels to resize them; the layout and window size are kept in
`~/.config/hektor/monitor.json`, `r` resets them. Mouse wheel / PageUp / PageDown scroll the
conversation, End goes back to the latest, q / Esc quit.

How it gets there: a shell script runs on hektor over one SSH connection (`REMOTE` in `monitor.py`),
sends the service status every 2 s, the machine's CPU time and RAM every second and the pictures as they come, and follows `~/.local/state/hektor/events.jsonl`. The worker side (`worker.py`) reads monitor: the
two services from systemd, busy from whisper-server's CPU use (sampled every 0.25 s), the log from their
journal without whisper's model-loading lines.

## Console

Left: every noise, click one to play it (a new variation each click). Right: the mic level live, against
the background noise and the speech threshold the recorder would use; below, type a message and press
Enter (or Speak) to hear it with the current voice. Esc stops talking (or quits when quiet), Ctrl+Q quits.

## Files

```
run.sh        runs a tool with the app's venv (PYTHONPATH=app, python -m dev)
__main__.py   the tool list
monitor.py    the monitor window and its SSH feed
worker.py     the monitor's view of the worker: services, busy, CPU / RAM, log, start / stop
console.py    the dev console
mic.py        mic-test, wake-test
sounds.py     sound
faces.py      faces
screenshots.py  screenshots: the eyes as the app draws them, the monitor app with made-up data
```
