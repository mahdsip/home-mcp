"""Builds the familia-mcp server from configuration.

Each module registers its tools only when configured, so a household without
(say) PhotoPrism simply does not expose photo tools to the model.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from . import __version__, health, homeassistant, nextcloud, phonetrack, photos, plex, prometheus
from .config import (HealthConfig, HomeAssistantConfig, LocationsConfig, NextcloudConfig, PhotosConfig, PlexConfig,
                     PrometheusConfig, resolve_env)
from .locphotos import PhotoEvidence

logger = logging.getLogger("familia_mcp")

INSTRUCTIONS = (
    "Home data tools for the familIA assistant. Results reflect live state of the household's "
    "devices and services. Never guess values that a tool did not return."
)


def build_server(
    environ: dict[str, str] | None = None,
    *,
    prometheus_transport: httpx.AsyncBaseTransport | None = None,
    homeassistant_transport: httpx.AsyncBaseTransport | None = None,
    plex_transport: httpx.AsyncBaseTransport | None = None,
    photos_target: Any = None,
    nextcloud_transport: httpx.AsyncBaseTransport | None = None,
    nextcloud_now: Any = None,
    locations_transport: httpx.AsyncBaseTransport | None = None,
    nominatim_transport: httpx.AsyncBaseTransport | None = None,
    health_transport: httpx.AsyncBaseTransport | None = None,
    health_today: Any = None,
) -> MCPServer:
    """Create the server. `environ`, the transports and `photos_target` (an
    MCP server standing in for PhotoPrism's) are injectable for tests."""
    env = resolve_env(environ)
    server = MCPServer(name="familia-mcp", version=__version__, instructions=INSTRUCTIONS)
    enabled = []

    prom_cfg = PrometheusConfig.from_env(env)
    if prom_cfg:
        client = prometheus.PrometheusClient(prom_cfg, transport=prometheus_transport)
        prometheus.register(server, prometheus.Prometheus(client, prom_cfg.device_label))
        enabled.append("prometheus")

    ha_cfg = HomeAssistantConfig.from_env(env)
    if ha_cfg:
        ha = homeassistant.HomeAssistant(
            homeassistant.HomeAssistantClient(ha_cfg, transport=homeassistant_transport),
            homeassistant.AllowlistStore(ha_cfg.allowlist_file),
            confirm_ttl_s=ha_cfg.confirm_ttl_s,
        )
        homeassistant.register(server, ha)
        enabled.append("homeassistant")
    elif env.get("HA_URL") or env.get("HA_TOKEN"):
        logger.warning("Home Assistant disabled: set both HA_URL and HA_TOKEN")

    plex_cfg = PlexConfig.from_env(env)
    if plex_cfg:
        plex.register(
            server,
            plex.Plex(plex.PlexClient(plex_cfg, transport=plex_transport), plex_cfg.user_tokens),
        )
        enabled.append("plex")
    elif env.get("PLEX_URL") or env.get("PLEX_TOKEN"):
        logger.warning("Plex disabled: set both PLEX_URL and PLEX_TOKEN")

    photos_cfg = PhotosConfig.from_env(env)
    photos_connect = None  # shared with the locations module (photo evidence)
    if photos_cfg:
        connect = photos.server_connect(photos_target) if photos_target is not None else photos.http_connect(photos_cfg)
        photos_connect = connect
        photos.register(server, photos.Photos(connect))
        enabled.append("photos")
    elif env.get("PHOTOPRISM_URL") or env.get("PHOTOPRISM_TOKEN"):
        logger.warning("Photos disabled: set both PHOTOPRISM_URL and PHOTOPRISM_TOKEN")

    nc_cfg = NextcloudConfig.from_env(env)
    if nc_cfg:
        nextcloud.register(
            server,
            nextcloud.Nextcloud(nextcloud.DavClient(nc_cfg, transport=nextcloud_transport), nc_cfg, now=nextcloud_now),
        )
        enabled.append("nextcloud")
    elif env.get("NEXTCLOUD_URL") or env.get("NEXTCLOUD_APP_PASSWORD"):
        logger.warning("Nextcloud disabled: set NEXTCLOUD_URL, NEXTCLOUD_USER and NEXTCLOUD_APP_PASSWORD")

    loc_cfg = LocationsConfig.from_env(env)
    if loc_cfg:
        try:
            loc = build_locations(loc_cfg, locations_transport, nominatim_transport, photos_connect=photos_connect)
        except (OSError, ValueError, sqlite3.Error) as exc:
            # A broken index must not take the other modules down.
            logger.error("Locations disabled: cannot open %s (%s)", loc_cfg.db_path, exc)
        else:
            phonetrack.register(server, loc)
            enabled.append("locations")
    elif env.get("PHONETRACK_ROOT"):
        logger.warning("Locations disabled: PHONETRACK_ROOT needs the NEXTCLOUD_* settings too")

    health_cfg = HealthConfig.from_env(env)
    if health_cfg:
        health.register(
            server,
            health.Health(health.OpenWearablesClient(health_cfg, transport=health_transport), health_cfg,
                          today=health_today),
        )
        enabled.append("health")
    elif env.get("OPENWEARABLES_URL") or env.get("OPENWEARABLES_API_KEY"):
        logger.warning("Health disabled: set both OPENWEARABLES_URL and OPENWEARABLES_API_KEY")

    if enabled:
        logger.info("familia-mcp modules enabled: %s", ", ".join(enabled))
    else:
        logger.warning("familia-mcp started with no modules configured (check the env file)")
    return server


def build_locations(cfg: LocationsConfig, files_transport: httpx.AsyncBaseTransport | None = None,
                    nominatim_transport: httpx.AsyncBaseTransport | None = None,
                    nominatim_interval_s: float = 1.05, photos_connect: Any = None) -> phonetrack.Locations:
    index = phonetrack.Index(cfg.db_path)
    return phonetrack.Locations(
        cfg,
        phonetrack.FilesClient(cfg, transport=files_transport),
        index,
        phonetrack.Nominatim(cfg, index, transport=nominatim_transport, min_interval_s=nominatim_interval_s),
        photos=PhotoEvidence(photos_connect) if photos_connect is not None else None,
    )
