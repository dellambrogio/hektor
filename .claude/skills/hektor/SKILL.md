---
name: hektor
description: Work with Hektor running on the robot laptop hektor (HEKTOR_HOST in deploy.conf) - deploy ("push to hektor"), check it's running, read what it heard and said, logs, restart, per-machine settings, re-run setup. Use whenever the user mentions hektor, the robot laptop, pushing/deploying to hektor, or asks how the robot is doing.
---

# Hektor on hektor

Which machine it is lives in `deploy.conf` at the repo root (not committed; `deploy.conf.example`):
`HEKTOR_HOST` (user@machine, the `hektor` account) and `HEKTOR_ADMIN` (the admin account there). Below,
`HOST` is `HEKTOR_HOST` and `ADMIN@MACHINE` the admin on the same machine: read them from that file first.

hektor (the robot laptop) logs in automatically as `hektor` (no sudo) into GNOME; the user service `hektor.service`
runs the app full screen (`ubuntu/hektor-run` → `app/run.sh --kiosk`). `ADMIN` is the admin account.
The code there is an rsync copy of this repo in `~hektor/hektor` (no git). Details: `ubuntu/README.md`.

## "Push to hektor" = deploy

"Push to hektor" means deploy to hektor, **not** `git push`. From the repo root:

```
ubuntu/deploy.sh
```

It rsyncs `app/` and `ubuntu/` (uncommitted changes included), updates the venv if `requirements.txt`
changed, refreshes `hektor.service` and restarts it. Then check it came back:

```
ssh HOST 'systemctl --user is-active hektor; tail -3 ~/.local/state/hektor/events.jsonl'
```

A `start` event newer than the deploy means it's up. If it isn't active, look at the journal (below).

## How is it doing

All read-only, over SSH as `hektor` (key auth works; never needs a password):

```
ssh HOST 'systemctl --user status hektor --no-pager'                  # running?
ssh HOST 'grep -E "\"type\": \"(heard|reply|unclear|nothing|interrupted|error)\"" ~/.local/state/hektor/events.jsonl | tail -20'   # what it heard / said
ssh HOST 'tail -50 ~/.local/state/hektor/hektor.log'                  # app log
ssh HOST 'journalctl --user -u hektor -n 50 --no-pager'               # crashes, stderr
ssh HOST 'cat ~/.config/hektor/hektor.conf'                           # settings for this laptop
```

Events are JSON lines (`hektor/events.py` lists the types). `heard` has `text`, `lang`, `stt` (whisper
seconds), `stt_on` (where whisper ran: `worker`, `server` on hektor, `cli`); `reply` has `text`, `mood`, `claude` (seconds for the whole answer), `wait` (seconds from the
question to Hektor talking), `new` (new conversation).

For the user to watch live: `dev/run.sh monitor` (a window: running or not, state, conversation,
the screen with the eyes, camera feed, hektor's CPU and RAM, the worker with a start / stop button).
On hektor too: `~/hektor/dev/run.sh mic-test` etc. (deploys copy `dev/`). The camera picture is someone's room: check the feed by frame counts and sizes
(`dev.monitor.Feed(...).picture()`, with `PYTHONPATH=app`), not by looking at it, unless the user asks.

## Faces

Hektor knows people who added their face (the `f` key on hektor, or "remember me"; `app/hektor/faces.py`): fingerprints in
`~hektor/.local/share/hektor/faces.json`, biometric data: never copy it off hektor or print its numbers.
`ssh HOST 'cd hektor && dev/run.sh faces'` lists names; `dev/run.sh faces forget NAME` only when the user asks.

## The worker

monitor can do the speech to text for hektor (`worker/`, see `worker/README.md`): two user
services here, `hektor-whisper` and `hektor-tunnel` (`systemctl --user status hektor-whisper hektor-tunnel`).
When it's down hektor does it itself, so it's never needed for Hektor to work.

## Changing things

- **Restart / stop / start**: `ssh HOST 'systemctl --user restart hektor'`
- **Settings for hektor only** go in `~hektor/.config/hektor/hektor.conf` (overrides `app/hektor.conf`,
  deploys never touch it). Edit with a heredoc or `sed` over SSH, then restart. Current choices there:
  `WHISPER_THREADS=4`, `WHISPER_WORKER=http://127.0.0.1:8910/inference` (the worker, below),
  `SOUND_VOLUME=0.4` (the noises were too loud next to the voice), `CAMERA=no` (off for now).
- **Anything that changes the system** (packages, `setup.sh`, GDM, logind, reboot) needs `ADMIN`'s sudo
  password, which Claude doesn't have: ask the user to run `ubuntu/deploy.sh --setup` or
  `ssh -t ADMIN@MACHINE sudo reboot` in their own terminal.
- **Claude token**: `ubuntu/claude-token.sh` (interactive: browser login + paste) — the user runs it.

## Care

- Restarting makes Hektor start over on the robot (and the conversation resets); fine for deploys, but
  don't restart in a loop.
- Don't play sounds or speech on purpose to test things (on hektor or here) without asking: test with
  files, fakes and the logs instead. Starting the real app on hektor is fine — that's the robot.
- Don't put the token or passwords on command lines or in the chat.
