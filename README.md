# home-mcp

Home MCP server for the [familIA](https://github.com/mahdsip/familIA) voice assistant. One process, six optional
modules; each is enabled only when its settings are present.

The Python package and the installed command are still named `familia-mcp` (`familia_mcp`): the deployed host
(`/opt/familia-mcp`, the `familia-mcp` service user) and the familIA proxy's forced SSH command rely on that name.

| Module | Tools | Access |
|---|---|---|
| Prometheus | `prom_devices_overview`, `prom_device_status`, `prom_metric_history`, `prom_active_alerts` | read-only |
| Home Assistant | `ha_list_devices`, `ha_device_state`, `ha_device_history`, `ha_control` | lights and plugs in an allowlist |
| Plex | `plex_search`, `plex_recently_added`, `plex_continue_watching`, `plex_watch_history`, `plex_now_playing` | read-only |
| Photos (PhotoPrism) | `photos_search`, `photos_albums`, `photos_people`, `photos_on_this_day` | read-only |
| Nextcloud | `nc_calendars`, `nc_agenda`, `nc_tasks`, `nc_add_task`, `nc_complete_task`, `nc_create_event`, `nc_find_contact`, `nc_add_contact` | read + create; events and contacts after a spoken "yes" |
| Locations | `loc_was_i_at`, `loc_where_was_i`, `loc_list_trips`, `loc_trip_summary`, `loc_runs_near`, `loc_chargers_near` | read-only (Nextcloud files + Nominatim) |

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
- **Nextcloud**: CalDAV/CardDAV with a dedicated app password (Nextcloud cannot scope those, so the client is the
  boundary): only `PROPFIND`, `REPORT` and `PUT`, only below the user's calendar and address book homes, and every
  `PUT` is conditional: `If-None-Match: *` to create, `If-Match: <etag>` to mark a task done, so a change made
  meanwhile on a phone is never overwritten. Nothing is ever deleted. Tasks are written directly; appointments and
  contacts are parked and written by the internal `nc_confirm_action` after a spoken "yes" (the read-back catches
  speech-recognition mistakes in times and phone numbers). Events never get attendees, so no invitation emails.
  A self-signed server certificate is pinned (`NEXTCLOUD_CERT_FILE`) instead of disabling TLS verification.
- **Locations**: the same app password, but the files client only sends `PROPFIND` and `GET`, only below
  `PHONETRACK_ROOT` (paths with `..` are refused), files over 25 MB are skipped. The index is mode 600 (umask 077
  covers SQLite's `-wal`/`-shm` files). Coordinates only leave the house towards Nominatim (see below).
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
| Nextcloud | `NEXTCLOUD_URL`, `NEXTCLOUD_USER`, `NEXTCLOUD_APP_PASSWORD` | `NEXTCLOUD_CERT_FILE`, `NEXTCLOUD_CALENDARS`, `NEXTCLOUD_DEFAULT_CALENDAR`, `NEXTCLOUD_DEFAULT_TASKLIST`, `NEXTCLOUD_ADDRESSBOOK`, `NEXTCLOUD_TIMEZONE` |
| Locations | `PHONETRACK_ROOT` + the Nextcloud settings | `PHONETRACK_DAILY_FOLDER`, `PHONETRACK_SPORT_FOLDER`, `PHONETRACK_CHARGERS_FOLDER`, `PHONETRACK_DB`, `PHONETRACK_SYNC_INTERVAL_S`, `PHONETRACK_SYNC_BUDGET_S`, `NOMINATIM_URL`, `NOMINATIM_EMAIL`, `NOMINATIM_LANGUAGE` |

### Location history

Answers "¿pasé por la Gran Vía el 10 de noviembre?", "¿estuve en Segovia el 8?", "¿por dónde pasamos en el
viaje a Galicia?", "¿he corrido alguna vez por el Retiro?" and "¿qué cargadores hay cerca de Mérida?" from a
Nextcloud folder (`PHONETRACK_ROOT`, e.g. `Maps`):

| Folder | Content | Used by |
|---|---|---|
| `daily/` | PhoneTrack daily GPX exports (timed fixes, UTC) | `loc_was_i_at`, `loc_where_was_i` |
| any other | one trip per folder; dates in the folder name (`2023_08_07-14_Galicia` = 7 to 14 Aug, `2014_09_California` = the month, `2022_07_02_X` = that day) | all `loc_*` except runs/chargers |
| `SportTracker/` | Sports Tracker sessions | `loc_runs_near` only |
| `Cargadores/` | EV chargers as GPX waypoints | `loc_chargers_near` only |

- **Index.** Files are copied into SQLite (`PHONETRACK_DB`, mode 600, in a mode-700 directory because SQLite
  writes `-wal`/`-shm` files next to it). Build it once on the MCP host (a few minutes for ~1,200 files):
  `sudo -u familia-mcp /opt/familia-mcp/venv/bin/familia-mcp --sync-locations`. Tool calls then refresh it at
  most every `PHONETRACK_SYNC_INTERVAL_S`; Nextcloud folder ETags make an unchanged tree cost one `PROPFIND`.
- **Time budget.** The familIA proxy allows 20 s per tool call. A call waits at most `PHONETRACK_SYNC_BUDGET_S`
  (6 s, enough for the newest daily file) for the refresh, which then finishes in the background, and stops
  Nominatim lookups (1 per second) at `PHONETRACK_TOOL_BUDGET_S` (15 s), marking the answer truncated. A broken
  index disables only this module.
- **Local day and time** come from each fix's own coordinates (offline, `tzfpy`), so a day in California is
  California's day.
- **Dates of untimed files** (planned routes, tracks, marked places): the file name, else the folder name. A
  file-name date must fit the folder's range or year, which resolves `22_07_02` vs `2_7_22` and ignores export
  dates like `GraphHopper-Track-2023-08-05` in a 2019 trip.
- **Matching.** Areas (towns, parks): inside the boundary, or the straight line between two consecutive fixes
  (at most 2 h apart) crosses it. Streets and points of interest: within 50 m plus the fix's accuracy. A street
  is every OSM way with that name nearby, not just the first.
- **Evidence.** Each answer says what it rests on: `gps`, `gps_path` (between two fixes), `track`,
  `planned_route` or `marked_place`; only the first two prove presence. Days without any fix are reported as
  `days_without_gps_data`, distinct from "not there".
- **Privacy.** Nominatim (OpenStreetMap) turns names into shapes and, for day and trip summaries, coordinates
  into names, so the public instance sees those coordinates. Results are cached forever; requests are limited
  to one per second with an identifying User-Agent, per its usage policy. Set `NOMINATIM_URL` to a
  self-hosted instance to keep coordinates at home.

## Development

```bash
uv sync
uv run pytest -q          # fake HTTP backends and an in-process fake PhotoPrism MCP server
```

`mcp` is pinned (`mcp==2.3.0`): the 2.x API differs from 1.x, and the familIA proxy must use the same version.
Tool descriptions are in English; the assistant answers in the user's language.

## License

GPL-3.0-only. See [LICENSE](LICENSE).
