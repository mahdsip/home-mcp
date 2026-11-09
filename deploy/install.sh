#!/usr/bin/env bash
# Install or upgrade familia-mcp on the MCP Pi (idempotent).
#
# Run with sudo from a directory containing:
#   familia_mcp-*.whl  requirements.lock.txt  mcp.env.example  install.sh
# and optionally:
#   ha_allowlist.json  the household allowlist to install (validated first)
#   secrets.env        KEY=VALUE lines merged into mcp.env (e.g. PLEX_TOKEN),
#                      then deleted; values are never printed
#
#   sudo bash install.sh
#
# What it does:
#   1. system user `familia-mcp` (no password; shell only for the forced SSH command)
#   2. venv in /opt/familia-mcp with hash-verified dependencies + the wheel
#   3. /etc/familia/mcp.env (600) and an EMPTY ha_allowlist.json (nothing
#      exposed until you choose) -- existing files are never overwritten,
#      except by an explicit ha_allowlist.json / secrets.env next to this script
#   4. optionally asks for the Home Assistant token (hidden input, never echoed)
#   5. verification as `familia-mcp`: MCP handshake, tools, one read per
#      module, HA devices/entities. Output also goes to install.log.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP=/opt/familia-mcp
ETC=/etc/familia
SVC_USER=familia-mcp
SVC_HOME=/var/lib/familia-mcp
LOG="$SRC/install.log"

[ "$(id -u)" -eq 0 ] || { echo "Run with sudo." >&2; exit 1; }
WHEEL="$(ls "$SRC"/familia_mcp-*.whl 2>/dev/null | sort -V | tail -1)"
[ -n "$WHEEL" ] || { echo "No familia_mcp-*.whl next to this script." >&2; exit 1; }

# Create the log before tee does it in the background (otherwise chown races it).
: > "$LOG"
[ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER" "$LOG"
exec > >(tee -a "$LOG") 2>&1

echo "== 1. service user"
if id "$SVC_USER" >/dev/null 2>&1; then
  echo "exists"
else
  adduser --system --group --home "$SVC_HOME" --shell /bin/sh "$SVC_USER"
fi

echo "== 2. venv + packages"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
# PyPI only: Raspberry Pi OS adds piwheels in /etc/pip.conf, whose wheels
# would not match the locked hashes.
export PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1
PIP=("$APP/venv/bin/pip" install --quiet --index-url https://pypi.org/simple)
"${PIP[@]}" --require-hashes --only-binary=:all: -r "$SRC/requirements.lock.txt"
"${PIP[@]}" --no-deps --force-reinstall "$WHEEL"
"$APP/venv/bin/python" -c "import familia_mcp, mcp; from importlib.metadata import version; print('familia-mcp', familia_mcp.__version__, '| mcp', version('mcp'))"

echo "== 3. config"
install -d -m 755 "$ETC"
if [ -f "$ETC/mcp.env" ]; then
  echo "$ETC/mcp.env exists (kept)"
else
  install -o "$SVC_USER" -g "$SVC_USER" -m 600 "$SRC/mcp.env.example" "$ETC/mcp.env"
  echo "created $ETC/mcp.env"
fi
if [ -f "$ETC/ha_allowlist.json" ]; then
  echo "$ETC/ha_allowlist.json exists (kept)"
else
  printf '{\n  "allow": [],\n  "deny": []\n}\n' > "$ETC/ha_allowlist.json"
  chown "$SVC_USER:$SVC_USER" "$ETC/ha_allowlist.json"
  chmod 640 "$ETC/ha_allowlist.json"
  echo "created empty $ETC/ha_allowlist.json (no HA devices exposed yet)"
fi
if [ -f "$SRC/ha_allowlist.json" ]; then
  # Validate with the server's own parser: an invalid file would expose nothing.
  "$APP/venv/bin/python" -c "import json,sys; from familia_mcp.homeassistant import Allowlist; a=Allowlist.parse(json.load(open(sys.argv[1]))); print(f'allowlist ok: {len(a.rules)} rules')" "$SRC/ha_allowlist.json"
  cp -p "$ETC/ha_allowlist.json" "$ETC/ha_allowlist.json.bak-$(date +%Y%m%d-%H%M%S)"
  install -o "$SVC_USER" -g "$SVC_USER" -m 640 "$SRC/ha_allowlist.json" "$ETC/ha_allowlist.json"
  echo "installed $ETC/ha_allowlist.json (previous one kept as .bak-*)"
fi
if [ -f "$SRC/nextcloud.pem" ]; then
  # Pinned self-signed Nextcloud certificate (public, but must not be swappable by the service user).
  openssl x509 -in "$SRC/nextcloud.pem" -noout -fingerprint -sha256
  install -o root -g "$SVC_USER" -m 644 "$SRC/nextcloud.pem" "$ETC/nextcloud.pem"
  echo "installed $ETC/nextcloud.pem"
fi
if [ -f "$SRC/secrets.env" ]; then
  # Merge KEY=VALUE lines into mcp.env: replace existing keys, append new ones.
  # Only known keys; values are checked, never printed.
  "$APP/venv/bin/python" - "$SRC/secrets.env" "$ETC/mcp.env" <<'PY'
import re, sys
from familia_mcp.config import load_env_file
ALLOWED = {"HA_TOKEN", "PLEX_URL", "PLEX_TOKEN", "PLEX_USER_TOKENS", "PHOTOPRISM_URL", "PHOTOPRISM_TOKEN",
           "PROMETHEUS_BEARER_TOKEN", "PROMETHEUS_PASSWORD",
           "NEXTCLOUD_URL", "NEXTCLOUD_USER", "NEXTCLOUD_APP_PASSWORD", "NEXTCLOUD_CERT_FILE",
           "NEXTCLOUD_CALENDARS", "NEXTCLOUD_DEFAULT_CALENDAR", "NEXTCLOUD_DEFAULT_TASKLIST",
           "NEXTCLOUD_ADDRESSBOOK", "NEXTCLOUD_TIMEZONE"}
new = load_env_file(sys.argv[1])
# Calendar names may contain spaces and accents; everything else is a token or URL.
FREE_TEXT = {"NEXTCLOUD_CALENDARS", "NEXTCLOUD_DEFAULT_CALENDAR", "NEXTCLOUD_DEFAULT_TASKLIST"}
ok = lambda k, v: re.fullmatch(r"[^\n\r\"'`$\\]+" if k in FREE_TEXT else r"[A-Za-z0-9._:/=;@+-]+", v)  # noqa: E731
bad = sorted(set(new) - ALLOWED) + sorted(k for k, v in new.items() if not ok(k, v))
if bad:
    sys.exit(f"secrets.env rejected (unknown key or unexpected characters): {', '.join(bad)}")
lines, done = [], set()
for line in open(sys.argv[2], encoding="utf-8").read().splitlines():
    key = line.split("=", 1)[0].strip().lstrip("#").strip()
    if key in new and key not in done and ("=" in line):
        lines.append(f"{key}={new[key]}")  # replaces the value or a commented-out template line
        done.add(key)
    else:
        lines.append(line)
lines += [f"{k}={v}" for k, v in new.items() if k not in done]
open(sys.argv[2], "w", encoding="utf-8").write("\n".join(lines) + "\n")
print("merged into mcp.env:", ", ".join(sorted(new)))
PY
  chown "$SVC_USER:$SVC_USER" "$ETC/mcp.env"; chmod 600 "$ETC/mcp.env"
  shred -u "$SRC/secrets.env" 2>/dev/null || rm -f "$SRC/secrets.env"
  echo "secrets.env deleted"
fi

echo "== 4. Home Assistant token"
if grep -q '^HA_TOKEN=..*' "$ETC/mcp.env"; then
  echo "already set (kept)"
elif [ -t 0 ]; then
  read -rsp "Paste the HA long-lived token (Enter to skip): " HA_TOKEN_IN; echo
  if [ -n "$HA_TOKEN_IN" ]; then
    # HA tokens are JWTs; refuse anything else instead of escaping it.
    if [[ ! "$HA_TOKEN_IN" =~ ^[A-Za-z0-9._-]+$ ]]; then
      echo "That does not look like an HA token; not saved." >&2
    elif grep -q '^HA_TOKEN=' "$ETC/mcp.env"; then
      sed -i "s|^HA_TOKEN=.*|HA_TOKEN=${HA_TOKEN_IN}|" "$ETC/mcp.env"
      echo "saved"
    else
      echo "HA_TOKEN=${HA_TOKEN_IN}" >> "$ETC/mcp.env"
      echo "saved"
    fi
  else
    echo "skipped (HA module stays disabled)"
  fi
  unset HA_TOKEN_IN
else
  echo "not a terminal: skipped"
fi

echo "== 5. verification (as $SVC_USER)"
cd /  # the service user cannot read the sudo user's home
sudo -u "$SVC_USER" "$APP/venv/bin/python" - <<'PY'
import asyncio, json
from mcp import Client
from mcp.client.stdio import StdioServerParameters

def payload(res):
    sc = res.structured_content
    if isinstance(sc, dict):
        return sc
    return {"text": " ".join(getattr(c, "text", "") for c in res.content)}

async def main():
    params = StdioServerParameters(command="/opt/familia-mcp/venv/bin/familia-mcp")
    async with Client(params, read_timeout_seconds=20) as c:
        tools = (await c.list_tools()).tools
        print("tools:", ", ".join(sorted(t.name for t in tools)) or "(none)")
        names = {t.name for t in tools}
        if "prom_devices_overview" in names:
            r = await c.call_tool("prom_devices_overview", {})
            out = payload(r)
            if r.is_error:
                print("prometheus ERROR:", out)
            else:
                print(f"prometheus: {out['device_count']} devices, down={out['down']}, attention={out['attention']}")
                for d in out["devices"]:
                    print("  ", json.dumps(d, ensure_ascii=False))
        if "ha_list_devices" in names:
            r = await c.call_tool("ha_list_devices", {})
            out = payload(r)
            if r.is_error:
                print("home assistant ERROR:", out)
            else:
                print(f"home assistant: {out.get('count')} allowlisted devices")
                for d in out.get("devices", []):
                    print("  ", d.get("name"), "|", d.get("kind"), "|", d.get("state"),
                          "| confirm" if d.get("requires_confirmation") else "")
        if "plex_continue_watching" in names:
            for acc in [None, "Kids"]:
                r = await c.call_tool("plex_continue_watching", {"account": acc} if acc else {})
                out = payload(r)
                print(f"plex continue watching ({acc or 'owner'}):", "ERROR " + str(out) if r.is_error
                      else f"{out.get('count')} items")
        if "nc_calendars" in names:  # read-only check; nothing is written during install
            r = await c.call_tool("nc_calendars", {})
            out = payload(r)
            print("nextcloud:", "ERROR " + str(out) if r.is_error else
                  f"calendars={[x['name'] for x in out.get('calendars', [])]} "
                  f"task_lists={[x['name'] for x in out.get('task_lists', [])]}")
        if "photos_people" in names:
            r = await c.call_tool("photos_people", {})
            out = payload(r)
            print("photos:", "ERROR " + str(out) if r.is_error else f"{out.get('total')} people recognised")

asyncio.run(main())
PY
if grep -q '^HA_TOKEN=..*' "$ETC/mcp.env"; then
  echo "-- HA lights/switches (ALLOWED = exposed by the allowlist)"
  sudo -u "$SVC_USER" "$APP/venv/bin/familia-mcp" --list-ha-entities 2>/dev/null || echo "could not list HA entities"
fi
echo "== done. Log: $LOG"
