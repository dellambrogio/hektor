#!/bin/bash
# Make monitor Hektor's worker: whisper.cpp with whisper-server, the model, and two user services
# (whisper-server here, an SSH tunnel to hektor). Run here, as yourself, no sudo. Safe to re-run.
#
#   worker/setup.sh            set up and start the worker
#   worker/setup.sh --remove   stop and remove the services (whisper.cpp and the model stay)
set -e
cd "$(dirname "$0")/.."
REPO=$PWD
source ubuntu/host.sh
source worker/worker.conf
UNITS=~/.config/systemd/user

if [[ "$1" == --remove ]]; then
    systemctl --user disable --now hektor-tunnel.service hektor-whisper.service 2>/dev/null || true
    rm -f "$UNITS/hektor-tunnel.service" "$UNITS/hektor-whisper.service"
    systemctl --user daemon-reload
    echo "worker removed"
    exit
fi
HOST=$(host "")

if [[ ! -x ~/.local/bin/whisper-server ]]; then
    echo "building whisper.cpp"
    mkdir -p ~/.local/src ~/.local/bin
    [[ -d ~/.local/src/whisper.cpp ]] || git clone --depth 1 https://github.com/ggml-org/whisper.cpp ~/.local/src/whisper.cpp
    cd ~/.local/src/whisper.cpp
    cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF >/dev/null
    cmake --build build -j"$(nproc)" --target whisper-cli whisper-server >/dev/null
    cp build/bin/whisper-cli build/bin/whisper-server ~/.local/bin/
    cd "$REPO"
fi

models=~/.local/share/hektor/models
if [[ ! -f "$models/$WORKER_MODEL" ]]; then
    echo "downloading $WORKER_MODEL"
    mkdir -p "$models"
    curl -L --fail -# -o "$models/$WORKER_MODEL.part" "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$WORKER_MODEL"
    mv "$models/$WORKER_MODEL.part" "$models/$WORKER_MODEL"
fi

echo "checking SSH to $HOST"
ssh -o BatchMode=yes -o ConnectTimeout=5 "$HOST" true || { echo "can't ssh to $HOST without a password: ssh-copy-id $HOST"; exit 1; }

mkdir -p "$UNITS"
for unit in hektor-whisper hektor-tunnel; do
    sed "s|@REPO@|$REPO|g" "worker/$unit.service" > "$UNITS/$unit.service"
done
systemctl --user daemon-reload
systemctl --user enable -q hektor-whisper.service hektor-tunnel.service
systemctl --user restart hektor-whisper.service hektor-tunnel.service
echo "worker running: whisper-server on 127.0.0.1:$WORKER_PORT ($WORKER_MODEL), tunnel to $HOST"
echo "status: systemctl --user status hektor-whisper hektor-tunnel"
