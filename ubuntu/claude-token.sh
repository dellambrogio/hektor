#!/bin/bash
# Give Hektor on hektor a Claude login: a long-lived token from `claude setup-token` (claude.ai
# subscription, no API key), written to ~/.config/hektor/env there. Run on monitor.
#
#   ubuntu/claude-token.sh [HOST]         runs `claude setup-token` here, then asks you to paste the token
#   ubuntu/claude-token.sh [HOST] < file  takes the token from stdin instead
#
# HOST is user@machine, default $HEKTOR_HOST (from deploy.conf, see deploy.conf.example). The token never
# goes on a command line; it's checked with a one-line question to Claude on hektor, then Hektor is restarted.
set -e
cd "$(dirname "$0")/.."
source ubuntu/host.sh
HOST=$(host "$1")

if [[ -t 0 ]]; then
    claude setup-token
    echo
    read -rsp "Paste the token (sk-ant-oat...): " token
    echo
else
    read -r token
fi
token=${token//[[:space:]]/}
[[ $token == sk-ant-* ]] || { echo "that doesn't look like a token (sk-ant-...)"; exit 1; }

echo "writing it to $HOST:~/.config/hektor/env"
ssh "$HOST" 'set -e
    read -r token
    f=~/.config/hektor/env
    mkdir -p ~/.config/hektor
    umask 077
    touch "$f"
    grep -v "^CLAUDE_CODE_OAUTH_TOKEN=" "$f" > "$f.new" || true
    echo "CLAUDE_CODE_OAUTH_TOKEN=$token" >> "$f.new"
    mv "$f.new" "$f"
    chmod 600 "$f"' <<< "$token"

echo "checking that Claude answers on hektor..."
ssh "$HOST" 'set -a; source ~/.config/hektor/env; set +a
    ~/.local/bin/claude -p "Reply with just the word: ok" </dev/null' || { echo "Claude didn't answer with the token"; exit 1; }

if ssh "$HOST" 'systemctl --user is-active -q hektor'; then
    ssh "$HOST" 'systemctl --user restart hektor' && echo "restarted Hektor"
fi
