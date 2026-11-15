#!/bin/bash
# Runs every 5 minutes from launchd. Notifies once when the bridge has been
# unreachable or disconnected for 30+ minutes, and once when it recovers.
PORT="${WHATSAPP_BRIDGE_PORT:-8765}"
STATE="$HOME/Library/Caches/whatsapp-bridge-watchdog"
THRESHOLD=1800
now=$(date +%s)

body=$(curl -s --max-time 5 "http://127.0.0.1:$PORT/api/health")
if echo "$body" | grep -q '"service":"whatsapp-bridge"' && echo "$body" | grep -q '"status":"ok"'; then
  if [ -f "$STATE" ] && grep -q notified "$STATE"; then
    osascript -e 'display notification "Connected again" with title "WhatsApp bridge"'
  fi
  rm -f "$STATE"
  exit 0
fi

[ -f "$STATE" ] || echo "$now" > "$STATE"
since=$(head -1 "$STATE")
if [ $((now - since)) -ge $THRESHOLD ] && ! grep -q notified "$STATE"; then
  mins=$(( (now - since) / 60 ))
  osascript -e "display notification \"Unreachable or disconnected for ${mins} min on port $PORT\" with title \"WhatsApp bridge\""
  echo notified >> "$STATE"
  echo "$(date '+%F %T') watchdog: bridge down for ${mins}m"
fi
