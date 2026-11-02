#!/usr/bin/env bash
# List Home Assistant notify services (names only) and the HA version, using
# the token in /etc/familia/mcp.env without printing it.
# Run on the MCP Pi:  sudo bash ha_notify_services.sh > /tmp/familia-install/ha_notify.txt
set -euo pipefail
ENV_FILE=/etc/familia/mcp.env
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
url="$(grep -m1 '^HA_URL=' "$ENV_FILE" | cut -d= -f2-)"; url="${url%/}"
token="$(grep -m1 '^HA_TOKEN=' "$ENV_FILE" | cut -d= -f2-)"
hdr="$(mktemp)"; chmod 600 "$hdr"; trap 'rm -f "$hdr"' EXIT
printf 'Authorization: Bearer %s\n' "$token" > "$hdr"
echo "HA version: $(curl -s -m 8 -H @"$hdr" "$url/api/config" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("version"))')"
echo "notify services:"
curl -s -m 8 -H @"$hdr" "$url/api/services" | python3 -c '
import sys, json
for d in json.load(sys.stdin):
    if d.get("domain") == "notify":
        for name in sorted(d.get("services", {})):
            print("  notify." + name)'
