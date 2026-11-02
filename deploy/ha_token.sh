#!/usr/bin/env bash
# Check (and optionally replace) the Home Assistant token in /etc/familia/mcp.env
# without ever printing it. Run on the MCP Pi:  sudo bash ha_token.sh
set -euo pipefail
ENV_FILE=/etc/familia/mcp.env
[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }

value() { grep -m1 "^$1=" "$ENV_FILE" | cut -d= -f2- || true; }

check() {
  local url token hdr code
  url="$(value HA_URL)"; url="${url%/}"
  token="$(value HA_TOKEN)"
  echo "HA_URL=$url"
  echo "HA_TOKEN: length=${#token} starts_with=${token:0:3}  (HA tokens are ~180 chars and start with 'eyJ')"
  [ -n "$token" ] || return 1
  # Header from a private temp file so the token never shows up in `ps`.
  hdr="$(mktemp)"; chmod 600 "$hdr"
  printf 'Authorization: Bearer %s\n' "$token" > "$hdr"
  code="$(curl -s -o /dev/null -m 8 -w '%{http_code}' -H @"$hdr" "$url/api/" || echo 000)"
  rm -f "$hdr"
  echo "GET $url/api/ -> HTTP $code"
  [ "$code" = "200" ]
}

if check; then
  echo "Token OK."
  exit 0
fi
read -rsp "Token rejected or missing. Paste a new one (Enter to keep): " NEW; echo
[ -n "$NEW" ] || exit 1
if [[ ! "$NEW" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "That does not look like an HA token; not saved." >&2
  exit 1
fi
if grep -q '^HA_TOKEN=' "$ENV_FILE"; then
  sed -i "s|^HA_TOKEN=.*|HA_TOKEN=${NEW}|" "$ENV_FILE"
else
  echo "HA_TOKEN=${NEW}" >> "$ENV_FILE"
fi
unset NEW
check && echo "Token OK." || { echo "Still rejected."; exit 1; }
