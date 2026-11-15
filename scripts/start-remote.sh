#!/bin/sh
# Runs the bridge and the MCP server together; if either exits, the container
# exits so Railway restarts both. On first run the bridge prints a pairing QR
# code to the deploy logs.
set -e
: "${PUBLIC_URL:?set PUBLIC_URL to the public https URL of this service}"
: "${WHATSAPP_OWNER_PHONE:?set WHATSAPP_OWNER_PHONE to your number, digits only with country code}"
mkdir -p /app/whatsapp-bridge/store
(cd /app/whatsapp-bridge && exec ./whatsapp-bridge) &
BRIDGE=$!
(cd /app/whatsapp-mcp-server && exec uv run --frozen --no-dev python main.py) &
MCP=$!
trap 'kill $BRIDGE $MCP 2>/dev/null' TERM INT
while kill -0 $BRIDGE 2>/dev/null && kill -0 $MCP 2>/dev/null; do sleep 5; done
echo "a process exited; stopping container" >&2
kill $BRIDGE $MCP 2>/dev/null || true
exit 1
