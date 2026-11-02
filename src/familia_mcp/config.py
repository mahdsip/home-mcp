"""Configuration loading for familia-mcp.

The server is started by sshd through a forced command, so it does not get a
systemd EnvironmentFile. It reads its own KEY=VALUE env file instead
(default /etc/familia/mcp.env, override with FAMILIA_MCP_ENV_FILE). Values
already present in the process environment win over the file, which keeps
tests and ad-hoc runs simple.

No secrets live in the repo: tokens for the home services only exist in that
file on the MCP Pi (chmod 600, owned by the service user).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ENV_FILE = "/etc/familia/mcp.env"
_LABEL_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def load_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse a simple KEY=VALUE file. Missing file -> empty dict.

    Supports blank lines, '#' comments, optional 'export ' prefix and single or
    double quotes around the value. No variable expansion (on purpose).
    """
    p = Path(path)
    if not p.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def resolve_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Merge the env file under the process environment (process env wins)."""
    environ = dict(os.environ if environ is None else environ)
    env_file = environ.get("FAMILIA_MCP_ENV_FILE", DEFAULT_ENV_FILE)
    merged = load_env_file(env_file)
    merged.update(environ)
    return merged


@dataclass(frozen=True)
class PrometheusConfig:
    url: str
    device_label: str = "instance"
    timeout_s: float = 5.0
    bearer_token: str | None = None
    username: str | None = None
    password: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "PrometheusConfig | None":
        url = (env.get("PROMETHEUS_URL") or "").strip().rstrip("/")
        if not url:
            return None  # module disabled
        if not url.startswith(("http://", "https://")):
            raise ValueError("PROMETHEUS_URL must start with http:// or https://")
        label = (env.get("PROMETHEUS_DEVICE_LABEL") or "instance").strip()
        # The label name is interpolated into PromQL `by (...)` clauses, so it
        # must be a valid Prometheus label name and nothing else.
        if not _LABEL_RE.match(label):
            raise ValueError("PROMETHEUS_DEVICE_LABEL must be a valid Prometheus label name")
        return cls(
            url=url,
            device_label=label,
            timeout_s=float(env.get("PROMETHEUS_TIMEOUT_S") or 5.0),
            bearer_token=env.get("PROMETHEUS_BEARER_TOKEN") or None,
            username=env.get("PROMETHEUS_USERNAME") or None,
            password=env.get("PROMETHEUS_PASSWORD") or None,
        )


def _parse_named_tokens(raw: str, var: str) -> dict[str, str]:
    """'Kids=abc;Abuelos=def' -> {'Kids': 'abc', 'Abuelos': 'def'}."""
    out: dict[str, str] = {}
    for part in (raw or "").split(";"):
        if not part.strip():
            continue
        name, sep, token = part.partition("=")
        if not sep or not name.strip() or not token.strip():
            raise ValueError(f"{var} must look like 'Name=token;Other=token'")
        out[name.strip()] = token.strip()
    return out


@dataclass(frozen=True)
class PlexConfig:
    url: str
    token: str
    user_tokens: dict[str, str]
    timeout_s: float = 8.0

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "PlexConfig | None":
        url = (env.get("PLEX_URL") or "").strip().rstrip("/")
        token = (env.get("PLEX_TOKEN") or "").strip()
        if not url or not token:
            return None
        if not url.startswith(("http://", "https://")):
            raise ValueError("PLEX_URL must start with http:// or https://")
        return cls(
            url=url,
            token=token,
            user_tokens=_parse_named_tokens(env.get("PLEX_USER_TOKENS", ""), "PLEX_USER_TOKENS"),
            timeout_s=float(env.get("PLEX_TIMEOUT_S") or 8.0),
        )


@dataclass(frozen=True)
class PhotosConfig:
    """PhotoPrism's built-in MCP endpoint (<PHOTOPRISM_URL>/api/v1/mcp)."""

    url: str
    token: str
    # Generous: the first face/person query after a PhotoPrism restart is slow on a Pi.
    timeout_s: float = 45.0

    @property
    def mcp_url(self) -> str:
        return f"{self.url}/api/v1/mcp"

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "PhotosConfig | None":
        url = (env.get("PHOTOPRISM_URL") or "").strip().rstrip("/")
        token = (env.get("PHOTOPRISM_TOKEN") or "").strip()
        if not url or not token:
            return None
        if not url.startswith(("http://", "https://")):
            raise ValueError("PHOTOPRISM_URL must start with http:// or https://")
        return cls(url=url, token=token, timeout_s=float(env.get("PHOTOPRISM_TIMEOUT_S") or 45.0))


DEFAULT_HA_ALLOWLIST = "/etc/familia/ha_allowlist.json"


@dataclass(frozen=True)
class HomeAssistantConfig:
    url: str
    token: str
    allowlist_file: str = DEFAULT_HA_ALLOWLIST
    timeout_s: float = 5.0
    confirm_ttl_s: float = 90.0

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "HomeAssistantConfig | None":
        url = (env.get("HA_URL") or "").strip().rstrip("/")
        token = (env.get("HA_TOKEN") or "").strip()
        if not url or not token:
            # Disabled (the caller logs it). A half-configured HA must not
            # take the whole server down: the other modules keep working.
            return None
        if not url.startswith(("http://", "https://")):
            raise ValueError("HA_URL must start with http:// or https://")
        return cls(
            url=url,
            token=token,
            allowlist_file=env.get("HA_ALLOWLIST_FILE") or DEFAULT_HA_ALLOWLIST,
            timeout_s=float(env.get("HA_TIMEOUT_S") or 5.0),
        )
