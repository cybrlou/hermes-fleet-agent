#!/bin/sh
# Cybrtch Hermes fleet sidecar — Linux
# Lives outside the Hermes tree so hermes update does not stop it.
# Asks for hub address + token. Does not print the token.
set -eu
HUB_URL="${HUB_URL:-}"
AGENT_ID="${AGENT_ID:-}"
SITE="${SITE:-}"
FLEET_TOKEN="${FLEET_TOKEN:-}"

ask() {
  var="$1"; prompt="$2"; cur="$3"
  if [ -n "$cur" ]; then
    echo "$cur"
    return
  fi
  printf "%s: " "$prompt" >&2
  IFS= read -r val
  [ -n "$val" ] || { echo "Missing: $prompt" >&2; exit 1; }
  echo "$val"
}

HUB_URL=$(ask HUB_URL "Hub address (IP or URL, no token)" "$HUB_URL")
AGENT_ID=$(ask AGENT_ID "Agent id (e.g. fig-front)" "$AGENT_ID")
SITE=$(ask SITE "Site name (e.g. fig)" "$SITE")
FLEET_TOKEN=$(ask FLEET_TOKEN "Fleet token (will not be printed)" "$FLEET_TOKEN")

HUB_URL=$(echo "$HUB_URL" | sed 's:/*$::')
case "$HUB_URL" in
  http://*|https://*) ;;
  *:*) HUB_URL="http://$HUB_URL" ;;
  *) HUB_URL="http://$HUB_URL:56575" ;;
esac

if [ "$(id -u)" -eq 0 ]; then
  INSTALL_DIR=/opt/hermes-fleet-agent
else
  INSTALL_DIR="${HOME}/.local/share/hermes-fleet-agent"
fi

HERE=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
SRC="$HERE/src/agent.py"
[ -f "$SRC" ] || SRC="$HERE/agent.py"
[ -f "$SRC" ] || { echo "agent.py not found" >&2; exit 1; }
mkdir -p "$INSTALL_DIR"
cp "$SRC" "$INSTALL_DIR/agent.py"
umask 077
printf 'HUB_URL=%s\nAGENT_ID=%s\nSITE=%s\nFLEET_TOKEN=%s\nCHAT_TIMEOUT=180\n' \
  "$HUB_URL" "$AGENT_ID" "$SITE" "$FLEET_TOKEN" > "$INSTALL_DIR/agent.env"
chmod 600 "$INSTALL_DIR/agent.env"

PY=$(command -v python3 || command -v python)
"$PY" -m pip install --user websockets >/dev/null

if command -v systemctl >/dev/null 2>&1; then
  if [ "$(id -u)" -eq 0 ]; then
    UNIT=/etc/systemd/system/hermes-fleet-agent.service
    WANTED=multi-user.target
    SYS=systemctl
  else
    mkdir -p "${HOME}/.config/systemd/user"
    UNIT="${HOME}/.config/systemd/user/hermes-fleet-agent.service"
    WANTED=default.target
    SYS="systemctl --user"
  fi
  cat > "$UNIT" <<EOF
[Unit]
Description=Hermes fleet agent sidecar
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/agent.env
ExecStart=$PY $INSTALL_DIR/agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=$WANTED
EOF
  $SYS daemon-reload
  $SYS enable --now hermes-fleet-agent.service || true
fi
echo "Sidecar installed in $INSTALL_DIR (outside Hermes). Token not shown."
echo "Survives hermes update. If hub health fails from this host, the public IP is not on the allow list."
