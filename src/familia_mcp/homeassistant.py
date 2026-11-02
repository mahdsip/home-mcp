"""Home Assistant module: read and control allowlisted lights and plugs.

Control is restricted at three layers, all enforced HERE (not by the model):

  1. Domains: only `light` and `switch` exist in this module. Anything else in
     the allowlist is rejected when the file is loaded.
  2. Allowlist: `/etc/familia/ha_allowlist.json` on the MCP Pi decides which
     entities are visible at all, which actions each may take, and which need
     a spoken confirmation. Missing or invalid file -> nothing is exposed.
  3. Confirmation: for `confirm: true` devices, `ha_control` only parks the
     action and returns a single-use token. The tool that executes it,
     `ha_confirm_action`, is marked internal: the proxy never declares it to
     the model and calls it itself only after the user says "yes".

Allowlist format (patterns use shell-style wildcards on the object id):

    {
      "allow": [
        {"entity_id": "light.*"},
        {"entity_id": "switch.enchufe_*"},
        {"entity_id": "switch.enchufe_nas", "confirm": true,
         "actions": ["turn_on"], "aliases": ["nas"]}
      ],
      "deny": ["switch.enchufe_router"]
    }

Exact entity rules win over wildcard rules; `deny` wins over everything. The
file is re-read when it changes, no restart needed.

HA has no per-entity permissions for API tokens, so the allowlist is the
security boundary. Use a dedicated, non-admin HA user for the token anyway.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatchcase
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .config import HomeAssistantConfig
from .matching import resolve

logger = logging.getLogger("familia_mcp.homeassistant")

# The only domains and services this module can ever call.
DOMAIN_ACTIONS: dict[str, frozenset[str]] = {
    "light": frozenset({"turn_on", "turn_off", "toggle"}),
    "switch": frozenset({"turn_on", "turn_off", "toggle"}),
}
KIND = {"light": "light", "switch": "plug"}
INTERNAL_META = {"familia/internal": True}

MAX_DEVICES_PER_CALL = 20
MAX_HISTORY_CHANGES = 20
HISTORY_PERIODS = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}

_ENTITY_RE = re.compile(r"^(light|switch)\.[a-z0-9_]+$")
_PATTERN_RE = re.compile(r"^(light|switch)\.[a-z0-9_*?]+$")
_RULE_KEYS = {"entity_id", "actions", "confirm", "aliases"}

Action = Literal["turn_on", "turn_off", "toggle"]
HistoryPeriod = Literal["24h", "7d"]


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------
class AllowlistError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    pattern: str
    actions: frozenset[str]
    confirm: bool = False
    aliases: tuple[str, ...] = ()

    @property
    def exact(self) -> bool:
        return "*" not in self.pattern and "?" not in self.pattern


@dataclass(frozen=True)
class Allowlist:
    rules: tuple[Rule, ...] = ()
    deny: tuple[str, ...] = ()

    @classmethod
    def parse(cls, data: Any) -> "Allowlist":
        if not isinstance(data, dict) or set(data) - {"allow", "deny"}:
            raise AllowlistError("top level must be an object with 'allow' and optional 'deny'")
        rules = []
        for i, raw in enumerate(data.get("allow") or []):
            where = f"allow[{i}]"
            if not isinstance(raw, dict) or set(raw) - _RULE_KEYS:
                raise AllowlistError(f"{where}: unknown keys (allowed: {sorted(_RULE_KEYS)})")
            pattern = raw.get("entity_id")
            if not isinstance(pattern, str) or not _PATTERN_RE.match(pattern):
                raise AllowlistError(f"{where}: entity_id must be light.<id> or switch.<id> (wildcards allowed)")
            domain_actions = DOMAIN_ACTIONS[pattern.split(".", 1)[0]]
            actions = raw.get("actions", sorted(domain_actions))
            if not isinstance(actions, list) or not set(actions) <= domain_actions:
                raise AllowlistError(f"{where}: actions must be a subset of {sorted(domain_actions)}")
            confirm = raw.get("confirm", False)
            aliases = raw.get("aliases", [])
            if not isinstance(confirm, bool):
                raise AllowlistError(f"{where}: confirm must be true/false")
            if not isinstance(aliases, list) or not all(isinstance(a, str) and a.strip() for a in aliases):
                raise AllowlistError(f"{where}: aliases must be a list of names")
            rules.append(Rule(pattern, frozenset(actions), confirm, tuple(a.strip() for a in aliases)))
        deny = data.get("deny") or []
        if not isinstance(deny, list) or not all(isinstance(d, str) and _PATTERN_RE.match(d) for d in deny):
            raise AllowlistError("deny must be a list of light./switch. patterns")
        # Exact rules first so they override wildcard rules.
        rules.sort(key=lambda r: not r.exact)
        return cls(tuple(rules), tuple(deny))

    def rule_for(self, entity_id: str) -> Rule | None:
        if any(fnmatchcase(entity_id, d) for d in self.deny):
            return None
        return next((r for r in self.rules if fnmatchcase(entity_id, r.pattern)), None)


class AllowlistStore:
    """Loads the allowlist file and reloads it when its mtime changes.

    Fails closed: a missing file exposes nothing; an invalid file exposes
    nothing and every call reports the problem.
    """

    def __init__(self, path: str):
        self.path = path
        self._mtime: float | None = None
        self._allowlist = Allowlist()
        self._error: str | None = None

    def get(self) -> Allowlist:
        try:
            mtime = os.stat(self.path).st_mtime
        except FileNotFoundError:
            if self._mtime is not None or self._error is None:
                logger.warning("HA allowlist %s not found: no devices exposed", self.path)
            self._mtime, self._allowlist, self._error = None, Allowlist(), "missing"
            return self._allowlist
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                with open(self.path, encoding="utf-8") as fh:
                    self._allowlist = Allowlist.parse(json.load(fh))
                self._error = None
                logger.info("HA allowlist loaded: %d rules", len(self._allowlist.rules))
            except (OSError, ValueError) as exc:
                self._allowlist, self._error = Allowlist(), str(exc)
                logger.error("HA allowlist invalid, no devices exposed: %s", exc)
        if self._error and self._error != "missing":
            raise ToolError("The Home Assistant allowlist file is invalid; no devices are available.")
        return self._allowlist


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------
@dataclass
class Device:
    entity_id: str
    name: str
    state: str
    rule: Rule
    attributes: dict[str, Any] = field(default_factory=dict)
    last_changed: str | None = None

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def names(self) -> list[str]:
        # Match on what the user sees (friendly name) and the configured
        # aliases. The technical object id is only used when HA has no
        # friendly name (then `name` is derived from it anyway); otherwise
        # "salón" would silently pick light.salon over "Luz Salón Techo".
        return [self.name, *self.rule.aliases]

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "kind": KIND[self.domain],
            "state": self.state,
            "allowed_actions": sorted(self.rule.actions),
            "requires_confirmation": self.rule.confirm,
        }
        brightness = self.attributes.get("brightness")
        if self.domain == "light" and isinstance(brightness, (int, float)):
            out["brightness_pct"] = round(brightness * 100 / 255)
        return out


def _local_iso(value: str | None) -> str | None:
    """HA UTC timestamp -> MCP host local time (the Pi's configured zone)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone().isoformat(timespec="minutes")
    except ValueError:
        return value


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
class HomeAssistantClient:
    def __init__(self, cfg: HomeAssistantConfig, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=cfg.url,
            timeout=cfg.timeout_s,
            headers={"Authorization": f"Bearer {cfg.token}", "Content-Type": "application/json"},
            transport=transport,
        )

    async def _request(self, method: str, path: str, **kw) -> Any:
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise ToolError(f"Home Assistant is not reachable ({type(exc).__name__}).") from exc
        if resp.status_code == 401:
            raise ToolError("Home Assistant rejected the access token.")
        if resp.status_code >= 400:
            raise ToolError(f"Home Assistant returned HTTP {resp.status_code}.")
        try:
            return resp.json()
        except ValueError as exc:
            raise ToolError("Home Assistant returned a non-JSON response.") from exc

    async def states(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/states")
        return data if isinstance(data, list) else []

    async def call_service(self, domain: str, service: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        out = await self._request("POST", f"/api/services/{domain}/{service}", json=data)
        return out if isinstance(out, list) else []

    async def history(self, entity_id: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            f"/api/history/period/{start.isoformat()}",
            params={
                "filter_entity_id": entity_id,
                "end_time": end.isoformat(),
                "minimal_response": "",
                "no_attributes": "",
            },
        )
        return data[0] if isinstance(data, list) and data and isinstance(data[0], list) else []


# --------------------------------------------------------------------------
# Domain logic
# --------------------------------------------------------------------------
@dataclass
class _Pending:
    action: str
    devices: list[Device]
    data: dict[str, Any]
    expires: float


class HomeAssistant:
    def __init__(self, client: HomeAssistantClient, allowlist: AllowlistStore, confirm_ttl_s: float = 90.0):
        self.client = client
        self.allowlist = allowlist
        self.confirm_ttl_s = confirm_ttl_s
        self._pending: dict[str, _Pending] = {}

    async def devices(self) -> list[Device]:
        allow = self.allowlist.get()
        out = []
        for s in await self.client.states():
            entity_id = s.get("entity_id") or ""
            if not _ENTITY_RE.match(entity_id):
                continue
            rule = allow.rule_for(entity_id)
            if rule is None:
                continue
            attrs = s.get("attributes") or {}
            name = attrs.get("friendly_name") or entity_id.split(".", 1)[1].replace("_", " ")
            out.append(
                Device(entity_id, name, s.get("state", "unknown"), rule, attrs, s.get("last_changed"))
            )
        return sorted(out, key=lambda d: d.name.lower())

    @staticmethod
    def _resolve(name: str, devices: list[Device]) -> Device:
        return resolve(name, devices, names=lambda d: d.names, label=lambda d: d.name)

    async def list_devices(self) -> dict[str, Any]:
        devices = await self.devices()
        return {"count": len(devices), "devices": [d.summary() for d in devices]}

    async def device_state(self, name: str) -> dict[str, Any]:
        device = self._resolve(name, await self.devices())
        out = device.summary()
        out["last_changed"] = _local_iso(device.last_changed)
        return out

    async def control(self, names: list[str], action: str, brightness_pct: int | None) -> dict[str, Any]:
        if action not in DOMAIN_ACTIONS["light"]:
            raise ToolError(f"Unsupported action '{action}'.")
        if not names:
            raise ToolError("No device given.")
        if len(names) > MAX_DEVICES_PER_CALL:
            raise ToolError(f"At most {MAX_DEVICES_PER_CALL} devices per call.")
        if brightness_pct is not None and (action != "turn_on" or not 1 <= brightness_pct <= 100):
            raise ToolError("brightness_pct (1-100) is only valid with turn_on on lights.")

        devices = await self.devices()
        results: list[dict[str, Any]] = []
        to_run: list[Device] = []
        to_confirm: list[Device] = []
        seen: set[str] = set()
        for name in names:
            try:
                device = self._resolve(name, devices)
            except ToolError as exc:
                results.append({"device": name, "status": "error", "error": str(exc)})
                continue
            if device.entity_id in seen:
                continue
            seen.add(device.entity_id)
            if action not in device.rule.actions:
                results.append({"device": device.name, "status": "error", "error": f"'{action}' is not allowed for this device."})
            elif brightness_pct is not None and device.domain != "light":
                results.append({"device": device.name, "status": "error", "error": "Brightness only applies to lights."})
            elif device.rule.confirm:
                to_confirm.append(device)
            else:
                to_run.append(device)

        data = {"brightness_pct": brightness_pct} if brightness_pct is not None else {}
        results.extend(await self._execute(action, to_run, data))
        out: dict[str, Any] = {"results": results}
        if to_confirm:
            self._expire()
            token = secrets.token_urlsafe(12)
            self._pending[token] = _Pending(action, to_confirm, data, time.monotonic() + self.confirm_ttl_s)
            out["confirmation"] = {
                # The proxy strips the token and the tool name before the
                # result is sent to the model; it keeps them locally.
                "confirmation_token": token,
                "confirm_tool": "ha_confirm_action",
                "action": action,
                "devices": [d.name for d in to_confirm],
                "instructions": (
                    "These devices need the user's spoken confirmation. Ask the user a short yes/no "
                    "question naming the action and devices. Do NOT call any tool to confirm; the "
                    "user's yes/no is handled outside this conversation."
                ),
            }
            results.extend({"device": d.name, "status": "needs_confirmation"} for d in to_confirm)
        return out

    async def confirm(self, token: str) -> dict[str, Any]:
        self._expire()
        pending = self._pending.pop(token, None)
        if pending is None:
            raise ToolError("Unknown or expired confirmation.")
        return {"results": await self._execute(pending.action, pending.devices, pending.data)}

    def _expire(self) -> None:
        now = time.monotonic()
        for t in [t for t, p in self._pending.items() if p.expires < now]:
            del self._pending[t]

    async def _execute(self, action: str, devices: list[Device], data: dict[str, Any]) -> list[dict[str, Any]]:
        results = []
        for device in devices:
            try:
                changed = await self.client.call_service(
                    device.domain, action, {"entity_id": device.entity_id, **data}
                )
            except ToolError as exc:
                results.append({"device": device.name, "status": "error", "error": str(exc)})
                continue
            new_state = next((s.get("state") for s in changed if s.get("entity_id") == device.entity_id), None)
            logger.info("HA action %s on %s -> %s", action, device.entity_id, new_state)
            results.append({"device": device.name, "status": "done", "state": new_state})
        return results

    async def history(self, name: str, period: str) -> dict[str, Any]:
        if period not in HISTORY_PERIODS:
            raise ToolError(f"Unknown period '{period}'.")
        device = self._resolve(name, await self.devices())
        end = datetime.now(timezone.utc)
        start = end - HISTORY_PERIODS[period]
        entries = await self.client.history(device.entity_id, start, end)

        changes: list[tuple[datetime, str]] = []
        for e in entries:
            try:
                when = datetime.fromisoformat(e.get("last_changed") or "")
            except ValueError:
                continue
            state = e.get("state")
            if not changes or changes[-1][1] != state:
                changes.append((max(when, start), state))
        on_seconds = 0.0
        for i, (when, state) in enumerate(changes):
            until = changes[i + 1][0] if i + 1 < len(changes) else end
            if state == "on":
                on_seconds += (until - when).total_seconds()
        return {
            "device": device.name,
            "period": period,
            "current_state": device.state,
            "time_on_hours": round(on_seconds / 3600, 1),
            "times_turned_on": sum(1 for i, (_, s) in enumerate(changes) if s == "on" and i > 0),
            "recent_changes": [
                {"at": w.astimezone().isoformat(timespec="minutes"), "state": s}
                for w, s in changes[-MAX_HISTORY_CHANGES:]
            ],
        }


# --------------------------------------------------------------------------
# MCP tool registration
# --------------------------------------------------------------------------
_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
_CONTROL = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

DeviceArg = Annotated[
    str,
    Field(description="Light or plug name as the user said it (e.g. 'luz del salón'). Fuzzy-matched."),
]


def register(server: MCPServer, ha: HomeAssistant) -> None:
    @server.tool(
        name="ha_list_devices",
        description=(
            "Lights and plugs the assistant may see/control in Home Assistant, with current state "
            "(on/off/unavailable), brightness for lights, allowed actions and whether a spoken "
            "confirmation is required. Use for 'which lights are on?', 'what can you control?'."
        ),
        annotations=_READ_ONLY,
    )
    async def ha_list_devices() -> dict[str, Any]:
        return await ha.list_devices()

    @server.tool(
        name="ha_device_state",
        description="Current state of ONE light or plug, with brightness and when it last changed.",
        annotations=_READ_ONLY,
    )
    async def ha_device_state(device: DeviceArg) -> dict[str, Any]:
        return await ha.device_state(device)

    @server.tool(
        name="ha_control",
        description=(
            "Turn on, turn off or toggle one or more lights/plugs, optionally setting light brightness. "
            "Only call this when the user explicitly asked for the action. Some devices return "
            "'needs_confirmation': then ask the user a yes/no question and stop."
        ),
        annotations=_CONTROL,
    )
    async def ha_control(
        devices: Annotated[list[str], Field(description="Device names, one per light/plug.", min_length=1, max_length=MAX_DEVICES_PER_CALL)],
        action: Annotated[Action, Field(description="What to do.")],
        brightness_pct: Annotated[int | None, Field(description="Light brightness 1-100, only with turn_on.", ge=1, le=100)] = None,
    ) -> dict[str, Any]:
        return await ha.control(devices, action, brightness_pct)

    @server.tool(
        name="ha_device_history",
        description=(
            "On/off history of ONE light or plug: recent changes with local times, how many times it "
            "was turned on and total hours on. Use for 'when did the kitchen light turn on?'."
        ),
        annotations=_READ_ONLY,
    )
    async def ha_device_history(
        device: DeviceArg,
        period: Annotated[HistoryPeriod, Field(description="Look-back window ending now.")] = "24h",
    ) -> dict[str, Any]:
        return await ha.history(device, period)

    @server.tool(
        name="ha_confirm_action",
        description="INTERNAL: executes a parked action after the user confirmed it. Never exposed to the model.",
        annotations=_CONTROL,
        meta=INTERNAL_META,
    )
    async def ha_confirm_action(confirmation_token: str) -> dict[str, Any]:
        return await ha.confirm(confirmation_token)
