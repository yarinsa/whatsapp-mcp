#!/bin/bash
# Stop and remove the bridge and watchdog launchd agents. Leaves store/ untouched.
set -uo pipefail
UID_NUM="$(id -u)"
for label in com.yarin.whatsapp-bridge com.yarin.whatsapp-bridge-watchdog; do
  launchctl bootout "gui/$UID_NUM/$label" 2>/dev/null && echo "Stopped $label"
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
done
echo "Uninstalled. Logs remain at ~/Library/Logs/whatsapp-bridge.log"
