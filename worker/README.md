# The worker

monitor doing Hektor's speech to text for hektor: `whisper-server` with a better model
(`small.en`) here, which hektor reaches through an SSH tunnel. For a 5 s question it takes ~0.55 s,
against ~0.75 s for `base` on hektor itself.

```
worker/setup.sh            # builds whisper.cpp, downloads the model, installs and starts two user services
worker/setup.sh --remove   # stops and removes the services
```

- `hektor-whisper.service`: `whisper-server` on `127.0.0.1:8910` only (`whisper.sh`)
- `hektor-tunnel.service`: `ssh -N -R 8910:127.0.0.1:8910` to hektor (`tunnel.sh`), started again
  when it drops. It ends a dead tunnel left on hektor first (after a Wi-Fi drop sshd keeps it, and
  the port, for a long time).

On hektor, `WHISPER_WORKER=http://127.0.0.1:8910/inference` in `~/.config/hektor/hektor.conf`. Each
question goes to the worker first; if the tunnel's down (monitor asleep, away, logged out) the
connection is refused at once and hektor does it itself, no slower than without a worker. If the worker
fails or takes too long (2 s + 0.3 s per second of speech), hektor does it and leaves the worker
alone for a minute. The `heard` event and the monitor say where whisper ran (`worker`, `server`, `cli`).

Nothing is opened on the network: whisper-server listens on localhost, the audio goes through SSH. The
machine and user come from `deploy.conf`; the model, port and threads from `worker.conf` (run
`setup.sh` again after changing them).

| Check                    | How                                                           |
|--------------------------|---------------------------------------------------------------|
| running?                 | `systemctl --user status hektor-whisper hektor-tunnel`         |
| logs                     | `journalctl --user -u hektor-tunnel -u hektor-whisper`         |
| does hektor reach it?   | `grep worker ~/.local/state/hektor/hektor.log` there: "worker there" / "not there" |
