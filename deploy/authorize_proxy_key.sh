#!/usr/bin/env bash
# Authorize the OVOS proxy's SSH key on the MCP Pi, restricted to running
# familia-mcp (no shell, no PTY, no forwarding). Idempotent.
#   sudo bash authorize_proxy_key.sh "ssh-ed25519 AAAA... familia-proxy@openvoice"
set -euo pipefail
SVC_USER=familia-mcp
HOME_DIR=/var/lib/familia-mcp
CMD=/opt/familia-mcp/venv/bin/familia-mcp
PUB="${1:?pass the proxy public key line}"
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }

# Only a single, well-formed ed25519 public key is accepted.
if [[ ! "$PUB" =~ ^ssh-ed25519\ [A-Za-z0-9+/]{68}\ [A-Za-z0-9@._-]+$ ]]; then
  echo "Not a valid single ssh-ed25519 public key line." >&2; exit 1
fi
printf '%s\n' "$PUB" | ssh-keygen -lf - >/dev/null || { echo "ssh-keygen rejected the key." >&2; exit 1; }

install -d -o "$SVC_USER" -g "$SVC_USER" -m 700 "$HOME_DIR/.ssh"
AK="$HOME_DIR/.ssh/authorized_keys"
LINE="restrict,command=\"$CMD\" $PUB"
touch "$AK"
KEYBLOB="$(echo "$PUB" | awk '{print $2}')"
grep -v -F "$KEYBLOB" "$AK" > "$AK.tmp" || true
printf '%s\n' "$LINE" >> "$AK.tmp"
mv "$AK.tmp" "$AK"
chown "$SVC_USER:$SVC_USER" "$AK"; chmod 600 "$AK"
echo "authorized_keys for $SVC_USER:"
sed -E 's/(ssh-ed25519 [A-Za-z0-9+/]{12})[A-Za-z0-9+/]+/\1.../' "$AK"
printf '%s\n' "$PUB" | ssh-keygen -lf -
