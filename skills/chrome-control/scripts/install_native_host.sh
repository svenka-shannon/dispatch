#!/bin/bash
# Install Chrome Control native messaging host

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOST_NAME="com.dispatch.chrome_control"

EXTENSION_ID="${1:-}"
if [ -z "$EXTENSION_ID" ]; then
  echo "Usage: $0 <extension_id>" >&2
  echo "" >&2
  echo "Chrome's native messaging manifest does NOT accept wildcards in" >&2
  echo "allowed_origins — it must be the exact extension ID." >&2
  echo "" >&2
  echo "To find the ID: load $SCRIPT_DIR/../extension as an unpacked" >&2
  echo "extension at chrome://extensions/ (Developer Mode), then copy the" >&2
  echo "32-char ID shown under the extension." >&2
  exit 1
fi

# Create native host manifest
MANIFEST_DIR="$HOME/Library/Application Support/Google/Chrome/NativeMessagingHosts"
mkdir -p "$MANIFEST_DIR"

cat > "$MANIFEST_DIR/$HOST_NAME.json" << EOF
{
  "name": "$HOST_NAME",
  "description": "Chrome Control Native Messaging Host",
  "path": "$SCRIPT_DIR/native_host",
  "type": "stdio",
  "allowed_origins": [
    "chrome-extension://$EXTENSION_ID/"
  ]
}
EOF

# Create launcher script
cat > "$SCRIPT_DIR/native_host" << EOF
#!/bin/bash
exec /usr/bin/python3 "$SCRIPT_DIR/native_host.py"
EOF

chmod +x "$SCRIPT_DIR/native_host"
chmod +x "$SCRIPT_DIR/native_host.py"

echo "Native messaging host installed!"
echo "Manifest: $MANIFEST_DIR/$HOST_NAME.json"
echo "Host: $SCRIPT_DIR/native_host"
echo ""
echo "Next steps:"
echo "1. Go to chrome://extensions/"
echo "2. Enable Developer Mode"
echo "3. Click 'Load unpacked' and select: $SCRIPT_DIR"
