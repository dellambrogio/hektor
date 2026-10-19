#!/bin/bash
# Copy the code to hektor and restart Hektor there. Run on monitor.
#
#   ubuntu/deploy.sh [HOST]           copy app/, dev/ and ubuntu/, update the venv if needed, restart Hektor
#   ubuntu/deploy.sh --setup [HOST]   first time (or after changing setup.sh): copy, then run setup.sh
#                                     there with sudo as the admin account (asks for its password)
#
# HOST is user@machine, default $HEKTOR_HOST (from deploy.conf, see deploy.conf.example); that user
# runs Hektor and has no sudo. The admin account on the same machine is $HEKTOR_ADMIN, also from there.
# Uncommitted changes go too.
set -e
cd "$(dirname "$0")/.."
source ubuntu/host.sh
SETUP=
if [[ "$1" == --setup ]]; then SETUP=1; shift; fi
HOST=$(host "$1")

echo "copying to $HOST:hektor/"
rsync -az --delete --exclude .venv --exclude __pycache__ --exclude '*.pyc' app dev ubuntu "$HOST:hektor/"

if [[ $SETUP ]]; then
    user=${HOST%@*} machine=${HOST#*@} admin=$(setting HEKTOR_ADMIN)
    ssh -t "$admin@$machine" "sudo ~$user/hektor/ubuntu/setup.sh $user"
    exit
fi
ssh "$HOST" bash -s <<'REMOTE'
set -e
cd ~/hektor/app
if [[ ! -x .venv/bin/python ]]; then
    echo "no venv yet: run deploy.sh --setup first"; exit 1
fi
sha=$(sha256sum requirements.txt | cut -d' ' -f1)
if [[ "$sha" != "$(cat .venv/requirements.sha 2>/dev/null)" ]]; then
    echo "requirements changed: updating the venv"
    .venv/bin/pip install -q -r requirements.txt
    echo "$sha" > .venv/requirements.sha
fi
cp ../ubuntu/hektor.service ~/.config/systemd/user/
systemctl --user daemon-reload
if systemctl --user is-active -q graphical-session.target; then
    systemctl --user restart hektor && echo "restarted Hektor"
else
    echo "nobody logged in to GNOME: Hektor starts at the next login"
fi
REMOTE
