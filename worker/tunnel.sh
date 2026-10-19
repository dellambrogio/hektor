#!/bin/bash
# The tunnel from hektor to whisper-server here: 127.0.0.1:WORKER_PORT there is 127.0.0.1:WORKER_PORT
# here, for as long as this runs. Run by hektor-tunnel.service, which starts it again when it drops.
set -e
cd "$(dirname "$0")/.."
source ubuntu/host.sh
source worker/worker.conf
HOST=$(host "")
opts=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=5 -o ServerAliveCountMax=3)
# a tunnel that died with the Wi-Fi may still hold the port there (sshd doesn't notice for a long
# time): end it. sshd hides which process has the port, but a session without a command or terminal,
# like this one (-N), shows as just "sshd-session: USER": every other session has @notty or @pts/N.
user=${HOST%@*}
ssh "${opts[@]}" "$HOST" "pkill -u $user -xf 'sshd(-session)?: $user' && sleep 1; true"
exec ssh -N "${opts[@]}" -o ExitOnForwardFailure=yes -R "127.0.0.1:$WORKER_PORT:127.0.0.1:$WORKER_PORT" "$HOST"
