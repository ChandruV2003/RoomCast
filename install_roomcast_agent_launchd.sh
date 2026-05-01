#!/bin/sh
set -eu

usage() {
  cat <<'EOF'
Usage:
  install_roomcast_agent_launchd.sh --server-url URL --host-slug SLUG --token TOKEN [options]

Options:
  --repo-path PATH          Path to the RoomCast repo (defaults to script directory)
  --python-executable PATH  Python executable to run the agent (default: python3 in PATH)
  --poll-interval SECONDS   Poll interval in seconds (default: 1)
EOF
}

SERVER_URL=""
HOST_SLUG=""
TOKEN=""
REPO_PATH=""
PYTHON_EXECUTABLE="${PYTHON_EXECUTABLE:-python3}"
POLL_INTERVAL="1"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --server-url)
      SERVER_URL="$2"
      shift 2
      ;;
    --host-slug)
      HOST_SLUG="$2"
      shift 2
      ;;
    --token)
      TOKEN="$2"
      shift 2
      ;;
    --repo-path)
      REPO_PATH="$2"
      shift 2
      ;;
    --python-executable)
      PYTHON_EXECUTABLE="$2"
      shift 2
      ;;
    --poll-interval)
      POLL_INTERVAL="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [ -z "$SERVER_URL" ] || [ -z "$HOST_SLUG" ] || [ -z "$TOKEN" ]; then
  usage >&2
  exit 1
fi

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -z "$REPO_PATH" ]; then
  REPO_PATH="$SCRIPT_DIR"
fi
REPO_PATH="$(cd "$REPO_PATH" && pwd)"
AGENT_SCRIPT="$REPO_PATH/roomcast_agent.py"
RUNTIME_DIR="$REPO_PATH/runtime"
PLIST_PATH="$HOME/Library/LaunchAgents/org.ntc.roomcast.$HOST_SLUG.plist"
STDOUT_PATH="$RUNTIME_DIR/$HOST_SLUG-launchd.log"
STDERR_PATH="$RUNTIME_DIR/$HOST_SLUG-launchd.err.log"

if [ ! -f "$AGENT_SCRIPT" ]; then
  echo "roomcast_agent.py was not found in $REPO_PATH" >&2
  exit 1
fi

PYTHON_PATH="$(command -v "$PYTHON_EXECUTABLE" || true)"
if [ -z "$PYTHON_PATH" ]; then
  echo "Python executable was not found: $PYTHON_EXECUTABLE" >&2
  exit 1
fi

mkdir -p "$RUNTIME_DIR" "$HOME/Library/LaunchAgents"

cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>org.ntc.roomcast.$HOST_SLUG</string>
    <key>ProgramArguments</key>
    <array>
      <string>$PYTHON_PATH</string>
      <string>$AGENT_SCRIPT</string>
      <string>--server-url</string>
      <string>$SERVER_URL</string>
      <string>--host-slug</string>
      <string>$HOST_SLUG</string>
      <string>--token</string>
      <string>$TOKEN</string>
      <string>--poll-interval</string>
      <string>$POLL_INTERVAL</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_PATH</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>15</integer>
    <key>StandardOutPath</key>
    <string>$STDOUT_PATH</string>
    <key>StandardErrorPath</key>
    <string>$STDERR_PATH</string>
    <key>ProcessType</key>
    <string>Background</string>
  </dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/org.ntc.roomcast.$HOST_SLUG" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/org.ntc.roomcast.$HOST_SLUG"
launchctl kickstart -k "gui/$(id -u)/org.ntc.roomcast.$HOST_SLUG"

echo "Installed LaunchAgent org.ntc.roomcast.$HOST_SLUG"
echo "plist: $PLIST_PATH"
