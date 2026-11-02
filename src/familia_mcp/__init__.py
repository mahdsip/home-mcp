"""familIA home MCP server.

One process, several optional modules (Prometheus, Home Assistant, Plex,
PhotoPrism). A module is enabled only when its configuration is present, so
the same binary serves whatever the household has.
"""

__version__ = "0.2.0"
