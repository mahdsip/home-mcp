"""Fake Prometheus HTTP API for tests (httpx.MockTransport)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

TARGETS = [
    # Same host, two exporters -> must collapse into ONE device "pimonitor".
    {"labels": {"instance": "pimonitor.local:9100", "job": "node"}, "health": "up", "lastError": ""},
    {"labels": {"instance": "pimonitor.local:9090", "job": "prometheus"}, "health": "up", "lastError": ""},
    {"labels": {"instance": "pihole.local:9100", "job": "node"}, "health": "up", "lastError": ""},
    {
        "labels": {"instance": "192.168.1.50:9100", "job": "node"},
        "health": "down",
        "lastError": "context deadline exceeded",
    },
]

# metric family -> {instance: value}
VALUES = {
    "cpu": {"pimonitor.local:9100": 12.34, "pihole.local:9100": 95.0},
    "memory": {"pimonitor.local:9100": 40.0, "pihole.local:9100": 55.5},
    "disk": {"pimonitor.local:9100": 91.2, "pihole.local:9100": 30.0},
    "temperature": {"pimonitor.local:9100": 52.0, "pihole.local:9100": 61.0},
    "load": {"pimonitor.local:9100": 0.42, "pihole.local:9100": 1.1},
    "uptime": {"pimonitor.local:9100": 3 * 86400, "pihole.local:9100": 86400 / 2},
}


def metric_of(promql: str) -> str:
    if "node_cpu_seconds_total" in promql:
        return "cpu"
    if "MemAvailable" in promql:
        return "memory"
    if "node_filesystem_avail_bytes" in promql:
        return "disk"
    if "node_hwmon_temp_celsius" in promql:
        return "temperature"
    if "node_load1" in promql:
        return "load"
    if "node_boot_time_seconds" in promql:
        return "uptime"
    raise AssertionError(f"unexpected query {promql}")


class FakePrometheus:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.fail_with: int | None = None
        self.alerts: list[dict[str, Any]] = []

    def _ok(self, data: Any) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": data})

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with:
            return httpx.Response(self.fail_with, text="boom")
        path = request.url.path
        params = dict(request.url.params)
        if path == "/api/v1/targets":
            assert params.get("state") == "active"
            return self._ok({"activeTargets": TARGETS})
        if path == "/api/v1/alerts":
            return self._ok({"alerts": self.alerts})
        if path in ("/api/v1/query", "/api/v1/query_range"):
            promql = params["query"]
            self.queries.append(promql)
            values = VALUES[metric_of(promql)]
            selected = {k: v for k, v in values.items() if _selected(promql, k)}
            if path == "/api/v1/query":
                result = [
                    {"metric": {"instance": k}, "value": [1_700_000_000, str(v)]}
                    for k, v in selected.items()
                ]
                return self._ok({"resultType": "vector", "result": result})
            start, step = float(params["start"]), int(params["step"])
            result = [
                {
                    "metric": {"instance": k},
                    "values": [[start + i * step, str(v + i)] for i in range(5)],
                }
                for k, v in selected.items()
            ]
            return self._ok({"resultType": "matrix", "result": result})
        return httpx.Response(404)


def _selected(promql: str, instance: str) -> bool:
    if 'instance="' not in promql and "instance=~" not in promql:
        return True
    return instance in promql.replace("\\\\", "\\").replace("\\.", ".")


@pytest.fixture
def fake_prom() -> FakePrometheus:
    return FakePrometheus()


@pytest.fixture
def prom_env(tmp_path) -> dict[str, str]:
    # Point at a non-existent env file so the developer's /etc is never read.
    return {
        "FAMILIA_MCP_ENV_FILE": str(tmp_path / "missing.env"),
        "PROMETHEUS_URL": "http://pimonitor.local:9090",
    }


def structured(result) -> dict[str, Any]:
    """Structured payload of a CallToolResult (falls back to JSON text)."""
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)
