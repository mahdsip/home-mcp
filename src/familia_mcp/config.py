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


def host_timezone() -> str:
    """IANA name of the host's time zone (Debian: /etc/timezone or the
    /etc/localtime symlink). UTC only if neither is available."""
    try:
        name = Path("/etc/timezone").read_text(encoding="utf-8").strip()
        if name:
            return name
    except OSError:
        pass
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


def _csv(raw: str | None) -> tuple[str, ...]:
    return tuple(p.strip() for p in (raw or "").split(",") if p.strip())


@dataclass(frozen=True)
class NextcloudConfig:
    """Nextcloud calendars, tasks (CalDAV) and contacts (CardDAV)."""

    url: str
    user: str
    app_password: str
    timezone: str
    cert_file: str | None = None        # pinned self-signed certificate (PEM)
    calendars: tuple[str, ...] = ()     # display names to expose; empty = all writable ones
    default_calendar: str | None = None
    default_tasklist: str | None = None
    addressbook: str = "contacts"       # Nextcloud's default address book URI
    timeout_s: float = 10.0
    confirm_ttl_s: float = 90.0

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "NextcloudConfig | None":
        url = (env.get("NEXTCLOUD_URL") or "").strip().rstrip("/")
        user = (env.get("NEXTCLOUD_USER") or "").strip()
        password = (env.get("NEXTCLOUD_APP_PASSWORD") or "").strip()
        if not url or not user or not password:
            return None
        if not url.startswith(("http://", "https://")):
            raise ValueError("NEXTCLOUD_URL must start with http:// or https://")
        if not re.fullmatch(r"[A-Za-z0-9._@-]+", user):
            raise ValueError("NEXTCLOUD_USER has unexpected characters")
        cert = (env.get("NEXTCLOUD_CERT_FILE") or "").strip() or None
        if cert and not Path(cert).is_file():
            raise ValueError(f"NEXTCLOUD_CERT_FILE {cert} does not exist")
        tz = (env.get("NEXTCLOUD_TIMEZONE") or "").strip() or host_timezone()
        return cls(
            url=url,
            user=user,
            app_password=password,
            timezone=tz,
            cert_file=cert,
            calendars=_csv(env.get("NEXTCLOUD_CALENDARS")),
            default_calendar=(env.get("NEXTCLOUD_DEFAULT_CALENDAR") or "").strip() or None,
            default_tasklist=(env.get("NEXTCLOUD_DEFAULT_TASKLIST") or "").strip() or None,
            addressbook=(env.get("NEXTCLOUD_ADDRESSBOOK") or "contacts").strip(),
            timeout_s=float(env.get("NEXTCLOUD_TIMEOUT_S") or 10.0),
        )


# In its own directory (mode 700): SQLite creates -wal/-shm files next to the database.
DEFAULT_LOCATIONS_DB = "/var/lib/familia-mcp/locations/phonetrack.sqlite"
_FOLDER_NAME_RE = re.compile(r"^[^/\\]+$")


def _folder(env: dict[str, str], key: str, default: str) -> str:
    value = (env.get(key) or default).strip().strip("/")
    if not _FOLDER_NAME_RE.match(value) or value in (".", ".."):
        raise ValueError(f"{key} must be a single folder name")
    return value


@dataclass(frozen=True)
class LocationsConfig:
    """Location history (PhoneTrack daily GPX, trips, sport sessions, chargers)
    read from a Nextcloud folder over WebDAV, indexed locally in SQLite."""

    nextcloud: NextcloudConfig
    root: str                              # folder in the user's files, e.g. "Maps"
    daily_folder: str = "daily"
    sport_folder: str = "SportTracker"
    chargers_folder: str = "Cargadores"
    db_path: str = DEFAULT_LOCATIONS_DB
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    nominatim_email: str | None = None     # sent as `email=` per Nominatim's usage policy
    nominatim_language: str | None = None  # Accept-Language for place names; default: local names
    sync_interval_s: float = 900.0
    # The familIA proxy gives a tool call 20 s: sync inline for a few seconds (enough for
    # the newest daily file), continue in the background, and cap the Nominatim lookups.
    sync_budget_s: float = 6.0             # max seconds a tool call waits for the sync
    tool_budget_s: float = 15.0            # max seconds a tool call spends in total
    timeout_s: float = 20.0

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "LocationsConfig | None":
        raw_root = (env.get("PHONETRACK_ROOT") or "").strip().strip("/")
        if not raw_root:
            return None
        nc = NextcloudConfig.from_env(env)
        if nc is None:
            return None  # the caller logs it
        if any(part in ("", ".", "..") for part in raw_root.split("/")):
            raise ValueError("PHONETRACK_ROOT must be a folder path like 'Maps'")
        url = (env.get("NOMINATIM_URL") or cls.nominatim_url).strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError("NOMINATIM_URL must start with http:// or https://")
        return cls(
            nextcloud=nc,
            root=raw_root,
            daily_folder=_folder(env, "PHONETRACK_DAILY_FOLDER", "daily"),
            sport_folder=_folder(env, "PHONETRACK_SPORT_FOLDER", "SportTracker"),
            chargers_folder=_folder(env, "PHONETRACK_CHARGERS_FOLDER", "Cargadores"),
            db_path=(env.get("PHONETRACK_DB") or DEFAULT_LOCATIONS_DB).strip(),
            nominatim_url=url,
            nominatim_email=(env.get("NOMINATIM_EMAIL") or "").strip() or None,
            nominatim_language=(env.get("NOMINATIM_LANGUAGE") or "").strip() or None,
            sync_interval_s=float(env.get("PHONETRACK_SYNC_INTERVAL_S") or 900.0),
            sync_budget_s=float(env.get("PHONETRACK_SYNC_BUDGET_S") or 6.0),
            tool_budget_s=float(env.get("PHONETRACK_TOOL_BUDGET_S") or 15.0),
            timeout_s=float(env.get("PHONETRACK_TIMEOUT_S") or 20.0),
        )


_EMAIL_RE = re.compile(r"^[^@\s;=]+@[^@\s;=]+$")


@dataclass(frozen=True)
class HealthConfig:
    """Open Wearables REST API (self-hosted wearable data)."""

    url: str
    api_key: str
    people: dict[str, str]   # spoken name -> Open Wearables user email; empty = the only user
    timezone: str
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "HealthConfig | None":
        url = (env.get("OPENWEARABLES_URL") or "").strip().rstrip("/")
        key = (env.get("OPENWEARABLES_API_KEY") or "").strip()
        if not url or not key:
            return None
        if not url.startswith(("http://", "https://")):
            raise ValueError("OPENWEARABLES_URL must start with http:// or https://")
        people = _parse_named_tokens(env.get("OPENWEARABLES_USERS", ""), "OPENWEARABLES_USERS")
        bad = [n for n, e in people.items() if not _EMAIL_RE.match(e)]
        if bad:
            raise ValueError(f"OPENWEARABLES_USERS needs 'Name=email': check {', '.join(bad)}")
        return cls(
            url=url,
            api_key=key,
            people=people,
            timezone=(env.get("OPENWEARABLES_TIMEZONE") or "").strip() or host_timezone(),
            timeout_s=float(env.get("OPENWEARABLES_TIMEOUT_S") or 10.0),
        )


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
