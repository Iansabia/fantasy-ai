#!/bin/sh
# Turn every fantasy-ai launchd job off (unload + remove). Run: ~/Developer/fantasy-ai/deploy/launchd/off.sh
for f in "$HOME"/Library/LaunchAgents/com.fantasyai.*.plist; do
  [ -e "$f" ] || continue
  launchctl unload "$f" 2>/dev/null
  rm -f "$f"
  echo "removed $(basename "$f")"
done
