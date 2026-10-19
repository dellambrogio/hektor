#!/bin/bash
# whisper-server for Hektor, on 127.0.0.1 only: hektor reaches it through the tunnel (tunnel.sh).
# Run by hektor-whisper.service.
set -e
cd "$(dirname "$0")/.."
source worker/worker.conf
exec ~/.local/bin/whisper-server -m ~/.local/share/hektor/models/"$WORKER_MODEL" -l en -t "$WORKER_THREADS" -nt \
    --host 127.0.0.1 --port "$WORKER_PORT"
