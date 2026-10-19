#!/bin/bash
# Add (or with --remove, remove) the "Hektor mic" PipeWire noise suppression + echo cancellation,
# then restart PipeWire. Without it Hektor records from the plain mic. Safe to re-run.
set -e
cd "$(dirname "$0")"
DEST=~/.config/pipewire/pipewire.conf.d/hektor-echo-cancel.conf
if [[ "$1" == --remove ]]; then
    rm -f "$DEST"
else
    mkdir -p "$(dirname "$DEST")"
    cp pipewire/hektor-echo-cancel.conf "$DEST"
fi
systemctl --user restart pipewire pipewire-pulse wireplumber
sleep 2
if pw-cli ls Node | grep -q '"hektor_mic"'; then echo "Hektor mic: on"; else echo "Hektor mic: off"; fi
