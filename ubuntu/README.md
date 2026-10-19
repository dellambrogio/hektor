# hektor: the robot laptop

The laptop logs in automatically as `hektor` at boot, into GNOME, and a user service (`hektor.service`)
starts Hektor full screen there and starts it again if it crashes. The code is copied there from this
repo with rsync; there's no git on hektor.

## First time

On hektor, by hand: Ubuntu Desktop LTS with an admin account (`ADMIN` below, `HEKTOR_ADMIN` in `deploy.conf`) and a plain `hektor` account
(no sudo) that runs Hektor, then

```
sudo apt install openssh-server
```

Here: `cp deploy.conf.example deploy.conf` (repo root, not committed) and put the machine in it, then

```
ssh-copy-id hektor@MACHINE
ssh-copy-id ADMIN@MACHINE
ubuntu/deploy.sh --setup      # copies the code as hektor, runs setup.sh with the admin's sudo (asks its password)
ubuntu/claude-token.sh        # Claude login for Hektor (see below)
ssh -t ADMIN@MACHINE sudo reboot
```

`setup.sh` installs the packages, builds whisper.cpp, installs Claude Code, downloads the models, makes
the venv, adds the noise suppression, turns on the auto-login and the Hektor service, turns off GNOME's
screen blanking, lock screen and notification banners for `hektor`, ignores the lid, turns off suspend
and limits the battery charge to 80% (if the kernel can; else set "Primarily AC use" in the BIOS).
It's safe to run again.

`claude-token.sh`: nobody logs in to Claude on the robot, so it uses a long-lived token from
`claude setup-token` (the claude.ai subscription, no API key). The script runs that here, asks you to
paste the token, writes it to `~/.config/hektor/env` on hektor (`chmod 600`), checks Claude answers with
it there, and restarts Hektor.

## Updating

```
ubuntu/deploy.sh              # copy app/ and ubuntu/, update the venv if requirements.txt changed, restart
```

Uncommitted changes go too. After changing `setup.sh`, run `deploy.sh --setup` again. The machine comes from
`deploy.conf` (`HEKTOR_HOST`, `HEKTOR_ADMIN`); for once another one: `deploy.sh user@host`, or
`HEKTOR_HOST=user@host` / `HEKTOR_ADMIN=name` in the environment.

## Seeing the eyes from here

Desktop Sharing for `hektor` (Settings → System → Remote Desktop → Desktop Sharing, on hektor as
`hektor`), then RDP to hektor on port 3390 (e.g. with Remmina). Port 3389 is GNOME's Remote
Login, which opens a separate desktop instead of showing the screen. With the auto-login the login
keyring stays locked, and Desktop Sharing keeps its password there: if it doesn't come up after a reboot,
give the `hektor` login keyring an empty password (Passwords and Keys → Login → Change Password).

## Watching it

`dev/run.sh monitor` (here) shows whether Hektor is running on hektor, what it's doing, and
the conversation as it happens: what it heard and what it answered, with timings, plus what its screen
shows (the eyes) and what the camera sees (faces boxed), ~3 pictures a second each (`c` turns the camera
off). It follows `~/.local/state/hektor/events.jsonl` there over SSH. The app writes the pictures
(`$XDG_RUNTIME_DIR/hektor/screen.jpg`, `camera.jpg`, see `hektor/share.py`) only while a monitor is
watching, and deletes them a few seconds after. Drag the gaps between the panels to resize them; the
layout is kept in `~/.config/hektor/monitor.json` here, `r` resets it. It also shows hektor's CPU and RAM,
and the worker on monitor (`worker/`): running or not with a button to start and stop it (or `w`),
busy while it transcribes, monitor's CPU and RAM, and the worker's log.

## On hektor

| What                     | Where / how                                             |
|--------------------------|---------------------------------------------------------|
| switch hektor off        | the monitor app's **Shut down** (twice); the power button turns it on again |
| quit Hektor              | `Ctrl+Q`: back to the desktop; `systemctl --user start hektor` brings it back |
| restart / stop Hektor    | `systemctl --user restart hektor` / `systemctl --user stop hektor` |
| settings for this laptop | `~/.config/hektor/hektor.conf` (deploys don't touch it) |
| Claude token             | `~/.config/hektor/env`                                  |
| logs                     | `~/.local/state/hektor/hektor.log`, `journalctl --user -u hektor` |
| models                   | `~/.local/share/hektor`                                 |
| code                     | `~/hektor/app`, `~/hektor/ubuntu`                       |
