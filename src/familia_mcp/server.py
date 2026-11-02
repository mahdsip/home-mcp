"""Builds the familia-mcp server from configuration.

Each module registers its tools only when configured, so a household without
(say) PhotoPrism simply does not expose photo tools to the model.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

from . import __version__, homeassistant, photos, plex, prometheus
from .config import HomeAssistantConfig, PhotosConfig, PlexConfig, PrometheusConfig, resolve_env

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
    if photos_cfg:
        connect = photos.server_connect(photos_target) if photos_target is not None else photos.http_connect(photos_cfg)
        photos.register(server, photos.Photos(connect))
        enabled.append("photos")
    elif env.get("PHOTOPRISM_URL") or env.get("PHOTOPRISM_TOKEN"):
        logger.warning("Photos disabled: set both PHOTOPRISM_URL and PHOTOPRISM_TOKEN")

    if enabled:
        logger.info("familia-mcp modules enabled: %s", ", ".join(enabled))
    else:
        logger.warning("familia-mcp started with no modules configured (check the env file)")
    return server
