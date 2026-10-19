#!/bin/bash
# Turn this Ubuntu laptop into Hektor: run on hektor with sudo from the admin account, on the code
# deploy.sh copied over (deploy.sh --setup does both). Safe to re-run.
#
#   sudo ~hektor/hektor/ubuntu/setup.sh [USER]     USER = the account that runs Hektor (default: hektor)
#
# Installs the packages, whisper.cpp, Claude Code, the models and the venv for USER, logs USER into GNOME
# automatically at boot with Hektor full screen (a user service), and keeps the laptop awake.
set -e
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
USER_=${1:-hektor}
[[ -n "$USER_" && "$USER_" != root ]] || { echo "usage: sudo $0 USER"; exit 1; }
HOME_=$(getent passwd "$USER_" | cut -d: -f6)
UID_=$(id -u "$USER_")
CODE=$HOME_/hektor
[[ -d "$CODE/app" ]] || { echo "no code in $CODE: run deploy.sh first"; exit 1; }
as_user() { sudo -u "$USER_" -H env XDG_RUNTIME_DIR=/run/user/$UID_ PATH="$HOME_/.local/bin:$PATH" "$@"; }
step() { echo; echo "=== $*"; }

step "packages"
apt-get update -q
apt-get install -y -q python3-venv python3-dev ffmpeg espeak-ng pipewire-bin wireplumber v4l-utils \
    rsync curl unzip git build-essential cmake

step "whisper.cpp"
if ! as_user bash -c 'command -v whisper-cli && command -v whisper-server' >/dev/null; then
    as_user bash -c '
        set -e
        mkdir -p ~/.local/src ~/.local/bin
        cd ~/.local/src
        [[ -d whisper.cpp ]] || git clone --depth 1 https://github.com/ggml-org/whisper.cpp
        cd whisper.cpp
        cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DWHISPER_BUILD_TESTS=OFF
        cmake --build build -j"$(nproc)" --target whisper-cli whisper-server
        cp build/bin/whisper-cli build/bin/whisper-server ~/.local/bin/'
fi
as_user bash -c 'echo "whisper-cli: $(command -v whisper-cli), whisper-server: $(command -v whisper-server)"'

step "Claude Code"
if ! as_user bash -c 'command -v claude' >/dev/null; then
    as_user bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
fi
as_user bash -c 'claude --version'

step "models and venv"
as_user "$CODE/app/fetch-models.sh"
if [[ ! -x "$CODE/app/.venv/bin/python" ]]; then
    as_user python3 -m venv "$CODE/app/.venv"
fi
as_user "$CODE/app/.venv/bin/pip" install -q -r "$CODE/app/requirements.txt"
sha256sum "$CODE/app/requirements.txt" | cut -d' ' -f1 | as_user tee "$CODE/app/.venv/requirements.sha" >/dev/null

step "settings"
as_user mkdir -p "$HOME_/.config/hektor"
if [[ ! -f "$HOME_/.config/hektor/hektor.conf" ]]; then
    as_user tee "$HOME_/.config/hektor/hektor.conf" >/dev/null <<'EOF'
# Hektor settings for this machine, over ~/hektor/app/hektor.conf (deploys don't touch this file).
PREFERRED_MICS=""       # PipeWire node name prefixes; empty: a USB mic, else the built-in one
PREFERRED_CAMERAS=""    # empty: the first color camera
EOF
fi
if [[ ! -f "$HOME_/.config/hektor/env" ]]; then
    as_user tee "$HOME_/.config/hektor/env" >/dev/null <<'EOF'
# Loaded by the Hektor session. Get the token with `claude setup-token` (on any computer).
CLAUDE_CODE_OAUTH_TOKEN=
EOF
fi
chmod 600 "$HOME_/.config/hektor/env"

step "GNOME: no screen blanking or lock for $USER_"
as_user dbus-run-session -- bash -c '
    gsettings set org.gnome.desktop.session idle-delay 0
    gsettings set org.gnome.desktop.screensaver lock-enabled false
    gsettings set org.gnome.desktop.screensaver idle-activation-enabled false
    gsettings set org.gnome.desktop.lockdown disable-lock-screen true
    gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing
    gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type nothing
    gsettings set org.gnome.settings-daemon.plugins.power idle-dim false
    gsettings set org.gnome.desktop.notifications show-banners false'

step "noise suppression"
as_user "$CODE/app/audio-setup.sh" || echo "(PipeWire not running for $USER_: it's picked up at the next login)"

step "auto-login as $USER_, Hektor started with GNOME"
UNITS=$HOME_/.config/systemd/user
as_user mkdir -p "$UNITS/graphical-session.target.wants"
as_user cp "$CODE/ubuntu/hektor.service" "$UNITS/"
as_user ln -sf ../hektor.service "$UNITS/graphical-session.target.wants/hektor.service"
# from the earlier cage "Hektor" session
rm -f /usr/local/bin/hektor-session /usr/share/wayland-sessions/hektor.desktop
GDM=/etc/gdm3/custom.conf
if [[ -f $GDM ]]; then
    sed -i -E '/^#? *(AutomaticLoginEnable|AutomaticLogin) *=/d' $GDM
    sed -i "/^\[daemon\]/a AutomaticLoginEnable=True\nAutomaticLogin=$USER_" $GDM
fi
mkdir -p /var/lib/AccountsService/users
ACC=/var/lib/AccountsService/users/$USER_
touch "$ACC"
grep -q '^\[User\]' "$ACC" || echo "[User]" >> "$ACC"
sed -i -E '/^(Session|XSession)=/d' "$ACC"
sed -i '/^\[User\]/a Session=ubuntu' "$ACC"
usermod -aG video,audio,input "$USER_"

step "power: lid ignored, no suspend, battery charge limit"
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/hektor.conf <<'EOF'
# Hektor: the laptop is a robot, closing the lid or idling must not suspend it
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
EOF
systemctl mask -q sleep.target suspend.target hibernate.target hybrid-sleep.target
# always on AC: keep the battery at most 80% full so it doesn't wear out or swell
cat > /etc/systemd/system/hektor-battery.service <<'EOF'
[Unit]
Description=Hektor: limit the battery charge to 80%
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'for b in /sys/class/power_supply/BAT*; do [ -w $b/charge_control_end_threshold ] && echo 80 > $b/charge_control_end_threshold; done; true'
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q --now hektor-battery.service
if compgen -G "/sys/class/power_supply/BAT*/charge_control_end_threshold" >/dev/null; then
    echo "battery limit: $(cat /sys/class/power_supply/BAT*/charge_control_end_threshold)%"
else
    echo "battery limit: not settable from Linux here, set \"Primarily AC use\" in the BIOS"
fi

step "done"
grep -q '^CLAUDE_CODE_OAUTH_TOKEN=.' "$HOME_/.config/hektor/env" ||
    echo "Still to do: put the token from \`claude setup-token\` in $HOME_/.config/hektor/env"
echo "Reboot to start Hektor (logind changes need it too): sudo reboot"
