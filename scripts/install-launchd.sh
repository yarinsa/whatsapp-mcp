#!/bin/bash
# Build the bridge and install it as a launchd agent that starts at login and
# restarts on exit, plus a watchdog that notifies when the bridge is unreachable.
# Run once in a terminal first (cd whatsapp-bridge && go run .) to pair via QR.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
BRIDGE_DIR="$REPO/whatsapp-bridge"
PORT="${WHATSAPP_BRIDGE_PORT:-8765}"
AGENTS="$HOME/Library/LaunchAgents"
LOG="$HOME/Library/Logs/whatsapp-bridge.log"
UID_NUM="$(id -u)"
BRIDGE_LABEL="com.yarin.whatsapp-bridge"
WATCHDOG_LABEL="com.yarin.whatsapp-bridge-watchdog"

if [ ! -f "$BRIDGE_DIR/store/whatsapp.db" ]; then
  echo "No WhatsApp session in $BRIDGE_DIR/store. Pair first: cd whatsapp-bridge && go run ." >&2
  exit 1
fi

echo "Building bridge..."
(cd "$BRIDGE_DIR" && go build -o bridge-bin .)

mkdir -p "$AGENTS" "$(dirname "$LOG")"
render() {
  sed -e "s|__BRIDGE_DIR__|$BRIDGE_DIR|g" -e "s|__REPO__|$REPO|g" \
      -e "s|__PORT__|$PORT|g" -e "s|__LOG__|$LOG|g" "$1" > "$2"
  plutil -lint "$2" >/dev/null
}
render "$REPO/scripts/$BRIDGE_LABEL.plist.template" "$AGENTS/$BRIDGE_LABEL.plist"
render "$REPO/scripts/$WATCHDOG_LABEL.plist.template" "$AGENTS/$WATCHDOG_LABEL.plist"

# Retire an older hand-made agent for the same binary (kept as .disabled).
LEGACY="me.yarinsa.whatsapp-bridge"
if [ -f "$AGENTS/$LEGACY.plist" ]; then
  launchctl bootout "gui/$UID_NUM/$LEGACY" 2>/dev/null || true
  mv "$AGENTS/$LEGACY.plist" "$AGENTS/$LEGACY.plist.disabled"
  echo "Disabled legacy agent $LEGACY"
fi

# Replace any running instance (launchd or manual) so the port is free.
for label in "$BRIDGE_LABEL" "$WATCHDOG_LABEL"; do
  launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null || true
done
pkill -f "$BRIDGE_DIR/bridge-bin" 2>/dev/null && sleep 2 || true
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is held by another process:" >&2
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >&2
  echo "Pick another port: WHATSAPP_BRIDGE_PORT=<port> $0" >&2
  exit 1
fi

launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$BRIDGE_LABEL.plist"
launchctl bootstrap "gui/$UID_NUM" "$AGENTS/$WATCHDOG_LABEL.plist"

echo "Installed. Bridge on port $PORT, log at $LOG"
echo "Check: curl -s http://127.0.0.1:$PORT/api/health"
