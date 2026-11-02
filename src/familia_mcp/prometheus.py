"""Prometheus module: device health from node_exporter, discovered dynamically.

Devices are NOT configured here. They are whatever Prometheus is scraping right
now (`/api/v1/targets`), grouped by a label (default `instance`). Add or remove
a target in Prometheus and the tools see it on the next call.

Safety: the model never writes PromQL. Every query comes from the fixed
templates below; the only variable part is a device label value that has first
been matched against the live target list and is then escaped.

Tool descriptions and errors are in English (they are read by the model, not
by the user); the orchestrator answers in the user's language.
"""

from __future__ import annotations

import asyncio
import ipaddress
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .config import PrometheusConfig
from .matching import normalize, resolve  # noqa: F401  (normalize re-exported for callers)

# Hostname suffixes stripped from the spoken/display device name.
_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".localdomain", ".home.arpa")

Metric = Literal["cpu", "memory", "disk", "temperature", "load"]
Period = Literal["1h", "6h", "24h", "7d", "30d"]

PERIOD_SECONDS: dict[str, int] = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 24 * 3600,
    "30d": 30 * 24 * 3600,
}
# ~120 points per range keeps query_range cheap on a Pi and is plenty for
# min/max/avg; never below Prometheus' usual 15-60 s scrape interval.
_RANGE_POINTS = 120
_MIN_STEP_S = 60

UNITS: dict[str, str] = {
    "cpu": "percent",
    "memory": "percent",
    "disk": "percent",
    "temperature": "celsius",
    "load": "load_average_1m",
}

# Simple "needs attention" thresholds for the overview (percent / celsius).
ATTENTION_THRESHOLDS: dict[str, float] = {
    "cpu": 90.0,
    "memory": 90.0,
    "disk": 90.0,
    "temperature": 75.0,
}


# --------------------------------------------------------------------------
# PromQL helpers
# --------------------------------------------------------------------------
def escape_label_value(value: str) -> str:
    """Escape a value for use inside a PromQL double-quoted label matcher."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


_IDLE = 'mode="idle"'
_ROOT_FS = 'mountpoint="/"'
_TEMP_METRICS = '__name__=~"node_hwmon_temp_celsius|node_thermal_zone_temp"'


def _sel(*parts: str) -> str:
    inner = ",".join(p for p in parts if p)
    return "{" + inner + "}" if inner else ""


def build_query(metric: str, label: str, matcher: str = "") -> str:
    """PromQL for one metric, aggregated per device label.

    `matcher` is either "" (all devices) or an already-escaped
    `label="value"` string produced by `device_matcher`.
    """
    m = matcher
    if metric == "cpu":
        idle = _sel(_IDLE, m)
        return f"100 * (1 - avg by ({label}) (rate(node_cpu_seconds_total{idle}[5m])))"
    if metric == "memory":
        return (
            f"100 * (1 - max by ({label}) (node_memory_MemAvailable_bytes{_sel(m)}) "
            f"/ max by ({label}) (node_memory_MemTotal_bytes{_sel(m)}))"
        )
    if metric == "disk":
        root = _sel(_ROOT_FS, m)
        return (
            f"max by ({label}) (100 * (1 - node_filesystem_avail_bytes{root} "
            f"/ node_filesystem_size_bytes{root}))"
        )
    if metric == "temperature":
        return f"max by ({label}) ({_sel(_TEMP_METRICS, m)})"
    if metric == "load":
        return f"max by ({label}) (node_load1{_sel(m)})"
    if metric == "uptime":
        return f"max by ({label}) (time() - node_boot_time_seconds{_sel(m)})"
    raise ValueError(f"unknown metric {metric!r}")


# --------------------------------------------------------------------------
# Device naming / matching
# --------------------------------------------------------------------------
def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def display_name(raw: str, label: str) -> str:
    """Human-friendly name: drop the port and mDNS-style suffixes."""
    name = raw.strip()
    if label == "instance":
        if name.startswith("[") and "]" in name:  # [ipv6]:port
            name = name[1 : name.index("]")]
        elif name.count(":") == 1:
            host, _, port = name.partition(":")
            if port.isdigit():
                name = host
    if not _is_ip(name):
        lowered = name.lower()
        for suffix in _LOCAL_SUFFIXES:
            if lowered.endswith(suffix):
                name = name[: -len(suffix)]
                break
    return name


@dataclass
class Device:
    """One physical device. Several scrape targets of the same host (e.g.
    node_exporter on :9100 and Prometheus itself on :9090) collapse into one
    device with several raw label values (`keys`)."""

    name: str  # display name
    keys: list[str] = field(default_factory=list)  # raw label values in Prometheus
    jobs: list[dict[str, Any]] = field(default_factory=list)

    def value_from(self, values: dict[str, float]) -> float | None:
        for k in self.keys:
            if k in values:
                return values[k]
        return None

    @property
    def health(self) -> str:
        states = {j.get("health") for j in self.jobs}
        if states == {"up"}:
            return "up"
        if "up" in states:
            return "partial"
        return "down"


def resolve_device(query: str, devices: list[Device]) -> Device:
    """Match a spoken device name against the live device list."""
    return resolve(query, devices, names=lambda d: [d.name, *d.keys], label=lambda d: d.name)


def _round(value: float | None, digits: int = 1) -> float | None:
    if value is None or math.isnan(value) or math.isinf(value):
        return None
    return round(value, digits)


def _local_iso(ts: float) -> str:
    """Timestamp in the MCP host's local timezone (configured on the Pi)."""
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="minutes")


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
class PrometheusClient:
    def __init__(self, cfg: PrometheusConfig, transport: httpx.AsyncBaseTransport | None = None):
        headers = {"Accept": "application/json"}
        if cfg.bearer_token:
            headers["Authorization"] = f"Bearer {cfg.bearer_token}"
        auth = (cfg.username, cfg.password or "") if cfg.username else None
        self._http = httpx.AsyncClient(
            base_url=cfg.url,
            timeout=cfg.timeout_s,
            headers=headers,
            auth=auth,
            transport=transport,
        )

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ToolError(f"Prometheus is not reachable ({type(exc).__name__}).") from exc
        if resp.status_code >= 400:
            raise ToolError(f"Prometheus returned HTTP {resp.status_code}.")
        try:
            body = resp.json()
        except ValueError as exc:
            raise ToolError("Prometheus returned a non-JSON response.") from exc
        if body.get("status") != "success":
            raise ToolError(f"Prometheus query failed: {body.get('error', 'unknown error')}.")
        return body.get("data")

    async def targets(self) -> list[dict[str, Any]]:
        data = await self._get("/api/v1/targets", {"state": "active"})
        return list((data or {}).get("activeTargets") or [])

    async def query(self, promql: str) -> list[dict[str, Any]]:
        data = await self._get("/api/v1/query", {"query": promql})
        return list((data or {}).get("result") or [])

    async def query_range(self, promql: str, start: float, end: float, step: int) -> list[dict[str, Any]]:
        data = await self._get(
            "/api/v1/query_range",
            {"query": promql, "start": f"{start:.0f}", "end": f"{end:.0f}", "step": str(step)},
        )
        return list((data or {}).get("result") or [])

    async def alerts(self) -> list[dict[str, Any]]:
        data = await self._get("/api/v1/alerts")
        return list((data or {}).get("alerts") or [])


# --------------------------------------------------------------------------
# Domain logic
# --------------------------------------------------------------------------
class Prometheus:
    def __init__(self, client: PrometheusClient, device_label: str = "instance"):
        self.client = client
        self.label = device_label

    async def devices(self) -> list[Device]:
        by_name: dict[str, Device] = {}
        for t in await self.client.targets():
            labels = t.get("labels") or {}
            key = labels.get(self.label)
            if not key:
                continue
            name = display_name(key, self.label)
            dev = by_name.setdefault(name.lower(), Device(name=name))
            if key not in dev.keys:
                dev.keys.append(key)
            dev.jobs.append(
                {
                    "job": labels.get("job"),
                    "health": t.get("health", "unknown"),
                    "last_error": t.get("lastError") or None,
                    "last_scrape": t.get("lastScrape"),
                }
            )
        return sorted(by_name.values(), key=lambda d: d.name.lower())

    def device_matcher(self, device: Device) -> str:
        """Escaped PromQL matcher selecting all raw label values of a device."""
        if len(device.keys) == 1:
            return f'{self.label}="{escape_label_value(device.keys[0])}"'
        pattern = "|".join(re.escape(k) for k in device.keys)
        return f'{self.label}=~"{escape_label_value(pattern)}"'

    async def _values_by_device(self, metric: str, matcher: str = "") -> dict[str, float]:
        return await self._values_by_device_expr(build_query(metric, self.label, matcher))

    async def _values_by_device_expr(self, promql: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for series in await self.client.query(promql):
            key = (series.get("metric") or {}).get(self.label)
            value = series.get("value") or [None, None]
            try:
                out[key] = float(value[1])
            except (TypeError, ValueError, IndexError):
                continue
        return out

    async def overview(self) -> dict[str, Any]:
        metrics = ("cpu", "memory", "disk", "temperature", "uptime")
        devices, *values = await asyncio.gather(
            self.devices(), *(self._values_by_device(m) for m in metrics)
        )
        per_metric = dict(zip(metrics, values))
        rows, attention = [], []
        for d in devices:
            row = _metrics_row(d, {m: d.value_from(per_metric[m]) for m in metrics})
            rows.append(row)
            attention.extend(_attention(row))
        return {
            "device_count": len(rows),
            "down": [r["name"] for r in rows if r["health"] == "down"],
            "attention": attention,
            "devices": rows,
            "units": _UNITS_OUT,
        }

    async def device_status(self, name: str) -> dict[str, Any]:
        device = resolve_device(name, await self.devices())
        matcher = self.device_matcher(device)
        metrics = ("cpu", "memory", "disk", "temperature", "load", "uptime")
        values = await asyncio.gather(*(self._values_by_device(m, matcher) for m in metrics))
        row = _metrics_row(device, {m: device.value_from(v) for m, v in zip(metrics, values)})
        row["load_1m"] = _round(row.pop("load", None), 2)
        row["jobs"] = device.jobs
        row["attention"] = _attention(row)
        row["units"] = _UNITS_OUT
        return row

    async def metric_history(self, name: str, metric: str, period: str) -> dict[str, Any]:
        if metric not in UNITS:
            raise ToolError(f"Unknown metric '{metric}'.")
        if period not in PERIOD_SECONDS:
            raise ToolError(f"Unknown period '{period}'.")
        device = resolve_device(name, await self.devices())
        seconds = PERIOD_SECONDS[period]
        end = time.time()
        step = max(_MIN_STEP_S, seconds // _RANGE_POINTS)
        expr = build_query(metric, self.label, self.device_matcher(device))
        # The ~120-point range gives the shape and timestamps; short peaks
        # fall between its samples, so the true extremes come from
        # max/min_over_time subqueries at 1-minute resolution.
        series, peak, trough = await asyncio.gather(
            self.client.query_range(expr, end - seconds, end, step),
            self._values_by_device_expr(f"max_over_time(({expr})[{period}:1m])"),
            self._values_by_device_expr(f"min_over_time(({expr})[{period}:1m])"),
        )
        points: list[tuple[float, float]] = []
        # With several targets per device only the exporter that has the
        # metric returns a series; keep the densest one.
        best = max(series, key=lambda s: len(s.get("values") or []), default=None)
        if best is not None:
            for ts, raw in best.get("values") or []:
                try:
                    v = float(raw)
                except (TypeError, ValueError):
                    continue
                if not (math.isnan(v) or math.isinf(v)):
                    points.append((float(ts), v))
        result: dict[str, Any] = {
            "device": device.name,
            "metric": metric,
            "unit": UNITS[metric],
            "period": period,
            "samples": len(points),
        }
        if not points:
            result["note"] = "No data for this metric and period (the device may not run node_exporter)."
            return result
        lo = min(points, key=lambda p: p[1])
        hi = max(points, key=lambda p: p[1])
        sub_max, sub_min = device.value_from(peak), device.value_from(trough)
        true_max = hi[1] if sub_max is None else max(hi[1], sub_max)
        true_min = lo[1] if sub_min is None else min(lo[1], sub_min)
        result.update(
            {
                "min": _round(true_min),
                # *_at come from the sampled range: approximate (± one step).
                "min_at": _local_iso(lo[0]),
                "max": _round(true_max),
                "max_at": _local_iso(hi[0]),
                "avg": _round(sum(v for _, v in points) / len(points)),
                "last": _round(points[-1][1]),
                "last_at": _local_iso(points[-1][0]),
            }
        )
        return result

    async def active_alerts(self) -> dict[str, Any]:
        alerts = []
        for a in await self.client.alerts():
            labels = a.get("labels") or {}
            ann = a.get("annotations") or {}
            key = labels.get(self.label)
            alerts.append(
                {
                    "alert": labels.get("alertname"),
                    "state": a.get("state"),
                    "severity": labels.get("severity"),
                    "device": display_name(key, self.label) if key else None,
                    "summary": ann.get("summary") or ann.get("description"),
                    "active_since": a.get("activeAt"),
                }
            )
        return {"count": len(alerts), "alerts": alerts}


_UNITS_OUT = {
    "cpu_percent": "percent",
    "memory_percent": "percent",
    "disk_root_percent": "percent",
    "temperature_celsius": "celsius",
    "uptime_days": "days",
}


def _metrics_row(device: Device, values: dict[str, float | None]) -> dict[str, Any]:
    if device.health == "down":
        # No fresh scrape: rate() windows still hold samples from before the
        # device went away (e.g. a suspended desktop), so any value would be
        # stale and misleading. Report nothing rather than old numbers.
        values = {k: None for k in values}
    uptime = values.get("uptime")
    row: dict[str, Any] = {
        "name": device.name,
        "health": device.health,
        "cpu_percent": _round(values.get("cpu")),
        "memory_percent": _round(values.get("memory")),
        "disk_root_percent": _round(values.get("disk")),
        "temperature_celsius": _round(values.get("temperature")),
        "uptime_days": _round(uptime / 86400 if uptime is not None else None),
    }
    if "load" in values:
        row["load"] = values.get("load")
    return row


def _attention(row: dict[str, Any]) -> list[str]:
    notes = []
    if row["health"] != "up":
        notes.append(f"{row['name']}: target {row['health']}")
    checks = (
        ("cpu", "cpu_percent"),
        ("memory", "memory_percent"),
        ("disk", "disk_root_percent"),
        ("temperature", "temperature_celsius"),
    )
    for metric, col in checks:
        value = row.get(col)
        if value is not None and value >= ATTENTION_THRESHOLDS[metric]:
            notes.append(f"{row['name']}: {col}={value}")
    return notes


# --------------------------------------------------------------------------
# MCP tool registration
# --------------------------------------------------------------------------
_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

DeviceArg = Annotated[
    str,
    Field(description="Device name as the user said it (e.g. 'pimonitor'). Fuzzy-matched against the monitored devices."),
]


def register(server: MCPServer, prom: Prometheus) -> None:
    @server.tool(
        name="prom_devices_overview",
        description=(
            "Health of ALL devices monitored by Prometheus (discovered live from its scrape targets): "
            "up/down, CPU %, memory %, root disk %, temperature, uptime, and a list of items needing attention. "
            "Use for questions like 'how are my devices?', 'is anything down?', 'which device is hottest?'."
        ),
        annotations=_READ_ONLY,
    )
    async def prom_devices_overview() -> dict[str, Any]:
        return await prom.overview()

    @server.tool(
        name="prom_device_status",
        description=(
            "Current detailed status of ONE monitored device: health per scrape job (with last error), CPU %, "
            "memory %, root disk %, temperature, 1-minute load and uptime."
        ),
        annotations=_READ_ONLY,
    )
    async def prom_device_status(device: DeviceArg) -> dict[str, Any]:
        return await prom.device_status(device)

    @server.tool(
        name="prom_metric_history",
        description=(
            "Summary (min, max, average, last, with local timestamps) of one metric for one device over a "
            "recent period. Use for 'how hot did X get today?', 'was the disk full last week?'."
        ),
        annotations=_READ_ONLY,
    )
    async def prom_metric_history(
        device: DeviceArg,
        metric: Annotated[Metric, Field(description="Which metric to summarize.")],
        period: Annotated[Period, Field(description="Look-back window ending now.")] = "24h",
    ) -> dict[str, Any]:
        return await prom.metric_history(device, metric, period)

    @server.tool(
        name="prom_active_alerts",
        description="Alerts currently pending or firing in Prometheus (empty if no alert rules are configured).",
        annotations=_READ_ONLY,
    )
    async def prom_active_alerts() -> dict[str, Any]:
        return await prom.active_alerts()
