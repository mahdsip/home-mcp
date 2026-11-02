"""Entry point: `familia-mcp` (stdio transport).

stdout carries the MCP protocol, so all logging goes to stderr. When spawned
through SSH, stderr reaches the proxy on the OVOS Pi, which logs it.

Admin helper (run on the MCP Pi, not over the forced-command SSH key):
    familia-mcp --list-ha-entities
prints every light/switch Home Assistant knows, marking which ones the current
allowlist exposes, to help write /etc/familia/ha_allowlist.json.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from .server import build_server


def _list_ha_entities() -> int:
    from . import homeassistant as ha
    from .config import HomeAssistantConfig, resolve_env

    cfg = HomeAssistantConfig.from_env(resolve_env())
    if cfg is None:
        print("Home Assistant is not configured (HA_URL / HA_TOKEN).", file=sys.stderr)
        return 1
    client = ha.HomeAssistantClient(cfg)
    store = ha.AllowlistStore(cfg.allowlist_file)
    states = asyncio.run(client.states())
    allow = store.get()
    for s in sorted(states, key=lambda s: s.get("entity_id", "")):
        entity_id = s.get("entity_id", "")
        if entity_id.split(".", 1)[0] not in ha.DOMAIN_ACTIONS:
            continue
        rule = allow.rule_for(entity_id)
        mark = "ALLOWED" + (" (confirm)" if rule and rule.confirm else "") if rule else "-"
        name = (s.get("attributes") or {}).get("friendly_name", "")
        print(f"{mark:<18} {entity_id:<45} {name}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="familia-mcp")
    parser.add_argument("--list-ha-entities", action="store_true", help="list HA lights/switches and exit")
    args = parser.parse_args()
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("FAMILIA_MCP_LOG_LEVEL", "INFO").upper(),
        format="familia-mcp %(levelname)s %(name)s: %(message)s",
    )
    if args.list_ha_entities:
        raise SystemExit(_list_ha_entities())
    build_server().run("stdio")


if __name__ == "__main__":
    main()
