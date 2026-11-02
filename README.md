# familia-mcp

Home MCP server for the [familIA](https://github.com/mahdsip/familIA) voice assistant. One process, four optional
modules; each is enabled only when its settings are present.

| Module | Tools | Access |
|---|---|---|
| Prometheus | `prom_devices_overview`, `prom_device_status`, `prom_metric_history`, `prom_active_alerts` | read-only |
| Home Assistant | `ha_list_devices`, `ha_device_state`, `ha_device_history`, `ha_control` | lights and plugs in an allowlist |
| Plex | `plex_search`, `plex_recently_added`, `plex_continue_watching`, `plex_watch_history`, `plex_now_playing` | read-only |
| Photos (PhotoPrism) | `photos_search`, `photos_albums`, `photos_people`, `photos_on_this_day` | read-only |

## How it runs

The server speaks MCP over **stdio**. It is not a network service: the familIA proxy starts it through SSH with a
restricted key whose forced command is the server
(`restrict,command="/opt/familia-mcp/venv/bin/familia-mcp"`). Nothing listens on the LAN.

```
voice (OVOS) -> familIA proxy -- SSH, forced command --> familia-mcp (this repo) -> Prometheus / HA / Plex / PhotoPrism
```

The tool calls themselves come from the familIA Lambda as "return of control": the model never reaches the home
services directly; the proxy runs the tool here and sends back the result.

## Safety model

- **Home Assistant**: only `light` and `switch` domains exist in the module. `/etc/familia/ha_allowlist.json` decides
  which entities are visible and which actions each may take (missing or invalid file -> nothing exposed). Devices
  with `"confirm": true` are only *parked*: the proxy asks the user and runs the internal `ha_confirm_action` tool
  itself after a spoken "yes"; that tool is never declared to the model. See `deploy/ha_allowlist.example.json`.
- **Plex**: the owner token can do anything on the server, so the client only issues `GET` requests to a fixed list
  of paths. "Continue watching" for Plex Home users uses each user's own token and never falls back to the owner's.
- **Photos**: a thin client of PhotoPrism's built-in read-only MCP endpoint (`/api/v1/mcp`). Use an app password
  with scope `mcp read photos albums people labels`; without `places` no coordinates are ever returned.
- Tokens live only in `/etc/familia/mcp.env` on the MCP host (mode 600, owned by the service user).

## Install / upgrade (Raspberry Pi OS, Python 3.11+)

On a dev machine:

```bash
uv run pytest -q
uv build --wheel
scp dist/familia_mcp-*.whl requirements.lock.txt deploy/install.sh deploy/mcp.env.example <mcp-host>:/tmp/familia-mcp/
# optional: your allowlist and a secrets.env (KEY=VALUE, merged into mcp.env and then deleted)
```

On the MCP host:

```bash
sudo bash /tmp/familia-mcp/install.sh
```

`install.sh` is idempotent: service user, venv with hash-verified dependencies (`requirements.lock.txt`), config
files that are never overwritten except by an explicit `ha_allowlist.json` / `secrets.env` next to it, and a final
check that calls one read tool per enabled module. Then authorize the proxy's key with
`deploy/authorize_proxy_key.sh` and restart the proxy so it re-reads the tool list.

Helpers: `deploy/ha_token.sh` (store the HA token without echoing it), `deploy/ha_notify_services.sh` (list HA
notify services).

## Configuration

All settings are `KEY=VALUE` lines in `/etc/familia/mcp.env` (override the path with `FAMILIA_MCP_ENV_FILE`);
see `deploy/mcp.env.example`.

| Module | Required | Optional |
|---|---|---|
| Prometheus | `PROMETHEUS_URL` | `PROMETHEUS_DEVICE_LABEL`, `PROMETHEUS_TIMEOUT_S`, bearer / basic auth |
| Home Assistant | `HA_URL`, `HA_TOKEN` | `HA_ALLOWLIST_FILE`, `HA_TIMEOUT_S` |
| Plex | `PLEX_URL`, `PLEX_TOKEN` | `PLEX_USER_TOKENS=Kids=<token>;...`, `PLEX_TIMEOUT_S` |
| Photos | `PHOTOPRISM_URL`, `PHOTOPRISM_TOKEN` | `PHOTOPRISM_TIMEOUT_S` |

## Development

```bash
uv sync
uv run pytest -q          # fake HTTP backends and an in-process fake PhotoPrism MCP server
```

`mcp` is pinned (`mcp==2.3.0`): the 2.x API differs from 1.x, and the familIA proxy must use the same version.
Tool descriptions are in English; the assistant answers in the user's language.

## License

GPL-3.0-only. See [LICENSE](LICENSE).
