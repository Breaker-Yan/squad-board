#!/bin/bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.squad-board"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BOARD_HOME="${SQUAD_BOARD_HOME:-$HOME/.squad-board}"
HOST="${SQUAD_BOARD_HOST:-127.0.0.1}"
PYTHON="$(command -v python3)"
HERDR="$(command -v herdr)" || { echo "herdr not found in PATH" >&2; exit 1; }

if [ "${1:-}" = "uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed $LABEL"
  exit 0
fi

mkdir -p "$BOARD_HOME" "$(dirname "$PLIST")"
# launchd starts with a minimal PATH; herdr and python must be reachable explicitly.
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PYTHON</string><string>$HERE/serve.py</string><string>$BOARD_HOME</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(dirname "$HERDR"):$(dirname "$PYTHON"):/usr/bin:/bin</string>
    <key>SQUAD_BOARD_HOST</key><string>$HOST</string>
  </dict>
  <key>WorkingDirectory</key><string>$HERE</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$BOARD_HOME/server.log</string>
  <key>StandardErrorPath</key><string>$BOARD_HOME/server.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "installed $LABEL → $PLIST (host $HOST, registry $BOARD_HOME)"
