#!/bin/bash
set -euo pipefail
domain="gui/$(id -u)"
if launchctl print "$domain/org.dom.studio" >/dev/null 2>&1; then
    launchctl bootout "$domain/org.dom.studio"
fi
rm -f "$HOME/Library/LaunchAgents/org.dom.studio.plist"
