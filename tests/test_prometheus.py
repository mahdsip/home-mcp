import asyncio

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from conftest import structured
from familia_mcp import prometheus as p
from familia_mcp.config import PrometheusConfig, load_env_file
from familia_mcp.server import build_server


# ---------------------------------------------------------------- pure helpers
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("pimonitor.local:9100", "pimonitor"),
        ("PiHole.LAN:9100", "PiHole"),
        ("192.168.1.50:9100", "192.168.1.50"),
        ("[fe80::1]:9100", "fe80::1"),
        ("nas", "nas"),
    ],
)
def test_display_name(raw, expected):
    assert p.display_name(raw, "instance") == expected


def test_normalize_handles_voice_variants():
    assert p.normalize("Pi Monitor") == p.normalize("pimonitor")
    assert p.normalize("Salón") == "salon"


def test_escape_label_value_blocks_promql_injection():
    evil = 'x"} or vector(1) or {a="'
    escaped = p.escape_label_value(evil)
    assert '"' not in escaped.replace('\\"', "")


def test_build_query_uses_matcher_and_label():
    q = p.build_query("cpu", "instance", 'instance="a:9100"')
    assert q == (
        '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle",instance="a:9100"}[5m])))'
    )
    with pytest.raises(ValueError):
        p.build_query("rm -rf", "instance")


def _devs():
    return [
        p.Device(name="pimonitor", keys=["pimonitor.local:9100"]),
        p.Device(name="pihole", keys=["pihole.local:9100"]),
        p.Device(name="nas", keys=["nas:9100"]),
    ]


def test_resolve_device_exact_fuzzy_ambiguous_unknown():
    devs = _devs()
    assert p.resolve_device("Pi Monitor", devs).name == "pimonitor"
    assert p.resolve_device("el nas", devs).name == "nas"  # spoken name contains device name
    with pytest.raises(ToolError, match="Ambiguous"):
        p.resolve_device("pi", devs)
    with pytest.raises(ToolError, match="Unknown device 'tostadora'. Known devices: nas, pihole, pimonitor"):
        p.resolve_device("tostadora", devs)


def test_config_disabled_without_url_and_validates_label():
    assert PrometheusConfig.from_env({}) is None
    with pytest.raises(ValueError):
        PrometheusConfig.from_env({"PROMETHEUS_URL": "pimonitor:9090"})
    with pytest.raises(ValueError):
        PrometheusConfig.from_env({"PROMETHEUS_URL": "http://x", "PROMETHEUS_DEVICE_LABEL": "a) or (b"})


def test_load_env_file(tmp_path):
    f = tmp_path / "mcp.env"
    f.write_text('# c\nexport PROMETHEUS_URL="http://h:9090"\nX=\'y\'\nBROKEN\n', encoding="utf-8")
    assert load_env_file(f) == {"PROMETHEUS_URL": "http://h:9090", "X": "y"}
    assert load_env_file(tmp_path / "nope") == {}


# ------------------------------------------------------ domain logic vs fake API
def _prom(fake):
    cfg = PrometheusConfig(url="http://pimonitor.local:9090")
    return p.Prometheus(p.PrometheusClient(cfg, transport=httpx.MockTransport(fake.handler)))


def test_devices_are_discovered_and_grouped_by_host(fake_prom):
    devs = asyncio.run(_prom(fake_prom).devices())
    assert [d.name for d in devs] == ["192.168.1.50", "pihole", "pimonitor"]
    pimon = devs[-1]
    assert sorted(pimon.keys) == ["pimonitor.local:9090", "pimonitor.local:9100"]
    assert pimon.health == "up"
    assert devs[0].health == "down"


def test_overview(fake_prom):
    out = asyncio.run(_prom(fake_prom).overview())
    assert out["device_count"] == 3
    assert out["down"] == ["192.168.1.50"]
    rows = {r["name"]: r for r in out["devices"]}
    assert rows["pimonitor"]["cpu_percent"] == 12.3
    assert rows["pimonitor"]["uptime_days"] == 3.0
    assert rows["192.168.1.50"]["cpu_percent"] is None
    assert "pimonitor: disk_root_percent=91.2" in out["attention"]
    assert "pihole: cpu_percent=95.0" in out["attention"]
    assert "192.168.1.50: target down" in out["attention"]


def test_down_device_reports_no_stale_metrics(fake_prom, monkeypatch):
    # Prometheus still returns a cpu value for a target that just went down.
    import conftest
    monkeypatch.setitem(conftest.VALUES["cpu"], "192.168.1.50:9100", 72.6)
    rows = {r["name"]: r for r in asyncio.run(_prom(fake_prom).overview())["devices"]}
    assert rows["192.168.1.50"]["health"] == "down"
    assert rows["192.168.1.50"]["cpu_percent"] is None
    assert rows["pimonitor"]["cpu_percent"] == 12.3  # healthy devices unaffected


def test_device_status_multi_target_matcher(fake_prom):
    out = asyncio.run(_prom(fake_prom).device_status("pi monitor"))
    assert out["name"] == "pimonitor"
    assert out["load_1m"] == 0.42
    assert {j["job"] for j in out["jobs"]} == {"node", "prometheus"}
    # Both raw instances are selected through an escaped regex matcher.
    assert all('instance=~"pimonitor\\\\.local:9100|pimonitor\\\\.local:9090"' in q or
               'instance=~"pimonitor\\\\.local:9090|pimonitor\\\\.local:9100"' in q
               for q in fake_prom.queries)


def test_metric_history_summary(fake_prom):
    out = asyncio.run(_prom(fake_prom).metric_history("pihole", "temperature", "24h"))
    assert out["samples"] == 5
    assert (out["min"], out["max"], out["avg"], out["last"]) == (61.0, 65.0, 63.0, 65.0)
    assert out["unit"] == "celsius"
    assert out["max_at"][:2] == "20"  # ISO date


def test_metric_history_catches_peaks_between_samples(fake_prom, monkeypatch):
    # A spike the 120-point range missed must still be the reported max.
    orig = fake_prom.handler

    def handler(request):
        q = dict(request.url.params).get("query", "")
        if request.url.path == "/api/v1/query" and q.startswith("max_over_time("):
            assert q.endswith("[24h:1m])")
            return httpx.Response(200, json={"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {"instance": "pihole.local:9100"}, "value": [0, "90.9"]}]}})
        return orig(request)

    fake_prom.handler = handler
    out = asyncio.run(_prom(fake_prom).metric_history("pihole", "temperature", "24h"))
    assert out["max"] == 90.9 and out["min"] == 61.0


def test_metric_history_no_data(fake_prom):
    out = asyncio.run(_prom(fake_prom).metric_history("192.168.1.50", "cpu", "1h"))
    assert out["samples"] == 0 and "note" in out


def test_prometheus_down_is_a_tool_error(fake_prom):
    fake_prom.fail_with = 503
    with pytest.raises(ToolError, match="HTTP 503"):
        asyncio.run(_prom(fake_prom).overview())


def test_active_alerts(fake_prom):
    fake_prom.alerts = [
        {
            "labels": {"alertname": "HighTemp", "instance": "pihole.local:9100", "severity": "warning"},
            "annotations": {"summary": "hot"},
            "state": "firing",
            "activeAt": "2026-10-31T10:00:00Z",
        }
    ]
    out = asyncio.run(_prom(fake_prom).active_alerts())
    assert out["count"] == 1 and out["alerts"][0]["device"] == "pihole"


# --------------------------------------------- full MCP round trip (in-process)
def test_mcp_tools_listed_and_callable(fake_prom, prom_env):
    server = build_server(prom_env, prometheus_transport=httpx.MockTransport(fake_prom.handler))

    async def go():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == {
                "prom_devices_overview",
                "prom_device_status",
                "prom_metric_history",
                "prom_active_alerts",
            }
            schema = tools["prom_metric_history"].input_schema
            assert schema["required"] == ["device", "metric"]
            ok = await client.call_tool("prom_device_status", {"device": "pihole"})
            bad = await client.call_tool("prom_device_status", {"device": "tostadora"})
            invalid = await client.call_tool(
                "prom_metric_history", {"device": "pihole", "metric": "bogus"}
            )
            return ok, bad, invalid

    ok, bad, invalid = asyncio.run(go())
    assert not ok.is_error and structured(ok)["name"] == "pihole"
    assert bad.is_error and "Unknown device" in bad.content[0].text
    assert invalid.is_error


def test_no_modules_when_unconfigured(tmp_path):
    server = build_server({"FAMILIA_MCP_ENV_FILE": str(tmp_path / "x")})

    async def go():
        async with Client(server) as client:
            return (await client.list_tools()).tools

    assert asyncio.run(go()) == []
