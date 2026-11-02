import asyncio
import json
import os

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from conftest import structured
from familia_mcp import homeassistant as ha
from familia_mcp.config import HomeAssistantConfig
from familia_mcp.matching import resolve
from familia_mcp.server import build_server

TOKEN = "test-token"

STATES = [
    {"entity_id": "light.salon", "state": "on", "attributes": {"friendly_name": "Luz Salón", "brightness": 128},
     "last_changed": "2026-10-31T17:00:00+00:00"},
    {"entity_id": "light.salon_techo", "state": "off", "attributes": {"friendly_name": "Luz Salón Techo"}},
    {"entity_id": "light.cocina", "state": "off", "attributes": {"friendly_name": "Luz Cocina"}},
    {"entity_id": "switch.enchufe_tele", "state": "on", "attributes": {"friendly_name": "Enchufe Tele"}},
    {"entity_id": "switch.enchufe_nas", "state": "on", "attributes": {"friendly_name": "Enchufe NAS"}},
    {"entity_id": "switch.enchufe_router", "state": "on", "attributes": {"friendly_name": "Enchufe Router"}},
    # Not plugs / not allowed domains: must never be visible.
    {"entity_id": "switch.adguard_protection", "state": "on", "attributes": {"friendly_name": "AdGuard"}},
    {"entity_id": "lock.puerta", "state": "locked", "attributes": {"friendly_name": "Puerta"}},
]

ALLOWLIST = {
    "allow": [
        {"entity_id": "light.*"},
        {"entity_id": "switch.enchufe_*"},
        {"entity_id": "switch.enchufe_nas", "confirm": True, "actions": ["turn_on", "turn_off"], "aliases": ["servidor"]},
    ],
    "deny": ["switch.enchufe_router"],
}


class FakeHA:
    def __init__(self):
        self.service_calls = []
        self.history_params = None
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"
        if self.status != 200:
            return httpx.Response(self.status)
        path = request.url.path
        if path == "/api/states":
            return httpx.Response(200, json=STATES)
        if path.startswith("/api/services/"):
            _, _, _, domain, service = path.split("/")
            body = json.loads(request.content)
            self.service_calls.append((domain, service, body))
            new = "off" if service == "turn_off" else "on"
            return httpx.Response(200, json=[{"entity_id": body["entity_id"], "state": new}])
        if path.startswith("/api/history/period/"):
            self.history_params = dict(request.url.params)
            start = path.rsplit("/", 1)[1]
            return httpx.Response(200, json=[[
                {"entity_id": "light.salon", "state": "off", "last_changed": start},
                {"state": "on", "last_changed": "2099-01-01T00:00:00+00:00"},  # future: clipped below
            ]])
        return httpx.Response(404)


@pytest.fixture
def fake_ha():
    return FakeHA()


@pytest.fixture
def allowlist_file(tmp_path):
    path = tmp_path / "ha_allowlist.json"
    path.write_text(json.dumps(ALLOWLIST), encoding="utf-8")
    return path


def _ha(fake, path):
    cfg = HomeAssistantConfig(url="http://homeassistant.local:8123", token=TOKEN)
    client = ha.HomeAssistantClient(cfg, transport=httpx.MockTransport(fake.handler))
    return ha.HomeAssistant(client, ha.AllowlistStore(str(path)))


# ------------------------------------------------------------------ allowlist
@pytest.mark.parametrize(
    "bad",
    [
        {"allow": [{"entity_id": "lock.puerta"}]},
        {"allow": [{"entity_id": "*.salon"}]},
        {"allow": [{"entity_id": "light.x", "actions": ["unlock"]}]},
        {"allow": [{"entity_id": "light.x", "confirm": "yes"}]},
        {"allow": [{"entity_id": "light.x", "colour": "red"}]},
        {"allow": [], "deny": ["climate.*"]},
        {"permit": []},
    ],
)
def test_allowlist_rejects_invalid(bad):
    with pytest.raises(ha.AllowlistError):
        ha.Allowlist.parse(bad)


def test_allowlist_precedence():
    allow = ha.Allowlist.parse(ALLOWLIST)
    assert allow.rule_for("switch.enchufe_nas").confirm is True  # exact beats wildcard
    assert allow.rule_for("switch.enchufe_tele").confirm is False
    assert allow.rule_for("switch.enchufe_router") is None  # deny wins
    assert allow.rule_for("switch.adguard_protection") is None


def test_missing_allowlist_exposes_nothing(fake_ha, tmp_path):
    out = asyncio.run(_ha(fake_ha, tmp_path / "nope.json").list_devices())
    assert out == {"count": 0, "devices": []}


def test_invalid_allowlist_fails_closed_and_reloads(fake_ha, allowlist_file):
    h = _ha(fake_ha, allowlist_file)
    assert asyncio.run(h.list_devices())["count"] == 5
    allowlist_file.write_text("{not json", encoding="utf-8")
    os.utime(allowlist_file, (1, 1))
    with pytest.raises(ToolError, match="allowlist file is invalid"):
        asyncio.run(h.list_devices())
    allowlist_file.write_text(json.dumps({"allow": [{"entity_id": "light.cocina"}]}), encoding="utf-8")
    os.utime(allowlist_file, (2, 2))
    assert [d["name"] for d in asyncio.run(h.list_devices())["devices"]] == ["Luz Cocina"]


# ---------------------------------------------------------------- read tools
def test_list_devices_only_allowlisted(fake_ha, allowlist_file):
    out = asyncio.run(_ha(fake_ha, allowlist_file).list_devices())
    names = [d["name"] for d in out["devices"]]
    assert names == ["Enchufe NAS", "Enchufe Tele", "Luz Cocina", "Luz Salón", "Luz Salón Techo"]
    salon = next(d for d in out["devices"] if d["name"] == "Luz Salón")
    assert salon["brightness_pct"] == 50 and salon["kind"] == "light"
    nas = next(d for d in out["devices"] if d["name"] == "Enchufe NAS")
    assert nas["kind"] == "plug" and nas["requires_confirmation"] is True


def test_device_matching_voice_forms(fake_ha, allowlist_file):
    devices = asyncio.run(_ha(fake_ha, allowlist_file).devices())
    pick = lambda q: resolve(q, devices, lambda d: d.names, lambda d: d.name).name  # noqa: E731
    assert pick("luz del salón") == "Luz Salón"  # exact skeleton beats "Salón Techo"
    assert pick("la tele") == "Enchufe Tele"
    assert pick("el servidor") == "Enchufe NAS"  # alias
    with pytest.raises(ToolError, match="Ambiguous"):
        pick("salón")
    with pytest.raises(ToolError, match="Unknown"):
        pick("router")  # denied -> not even known


def test_device_state(fake_ha, allowlist_file):
    out = asyncio.run(_ha(fake_ha, allowlist_file).device_state("luz salon"))
    assert out["state"] == "on" and out["last_changed"].startswith("2026-10-31")


def test_history(fake_ha, allowlist_file):
    out = asyncio.run(_ha(fake_ha, allowlist_file).history("luz salón", "24h"))
    assert fake_ha.history_params["filter_entity_id"] == "light.salon"
    assert "minimal_response" in fake_ha.history_params
    assert out["recent_changes"][0]["state"] == "off"
    assert out["times_turned_on"] == 1


# --------------------------------------------------------------- control
def test_control_runs_allowed_devices(fake_ha, allowlist_file):
    out = asyncio.run(_ha(fake_ha, allowlist_file).control(["luz cocina", "la tele"], "turn_on", None))
    assert fake_ha.service_calls == [
        ("light", "turn_on", {"entity_id": "light.cocina"}),
        ("switch", "turn_on", {"entity_id": "switch.enchufe_tele"}),
    ]
    assert [r["status"] for r in out["results"]] == ["done", "done"]
    assert "confirmation" not in out


def test_control_brightness_only_for_lights(fake_ha, allowlist_file):
    h = _ha(fake_ha, allowlist_file)
    asyncio.run(h.control(["luz cocina"], "turn_on", 30))
    assert fake_ha.service_calls[-1] == ("light", "turn_on", {"entity_id": "light.cocina", "brightness_pct": 30})
    out = asyncio.run(h.control(["la tele"], "turn_on", 30))
    assert out["results"][0]["status"] == "error"
    with pytest.raises(ToolError):
        asyncio.run(h.control(["luz cocina"], "turn_off", 30))


def test_control_never_reaches_unlisted_entities(fake_ha, allowlist_file):
    out = asyncio.run(_ha(fake_ha, allowlist_file).control(["router", "puerta", "adguard"], "turn_off", None))
    assert fake_ha.service_calls == []
    assert all(r["status"] == "error" for r in out["results"])


def test_control_respects_per_device_actions(fake_ha, allowlist_file):
    out = asyncio.run(_ha(fake_ha, allowlist_file).control(["nas"], "toggle", None))
    assert out["results"][0]["status"] == "error" and fake_ha.service_calls == []


def test_confirmation_flow(fake_ha, allowlist_file):
    h = _ha(fake_ha, allowlist_file)
    out = asyncio.run(h.control(["enchufe nas", "luz cocina"], "turn_off", None))
    # The unconfirmed device ran; the NAS plug is parked.
    assert fake_ha.service_calls == [("light", "turn_off", {"entity_id": "light.cocina"})]
    token = out["confirmation"]["confirmation_token"]
    assert out["confirmation"]["devices"] == ["Enchufe NAS"]
    done = asyncio.run(h.confirm(token))
    assert done["results"] == [{"device": "Enchufe NAS", "status": "done", "state": "off"}]
    assert fake_ha.service_calls[-1] == ("switch", "turn_off", {"entity_id": "switch.enchufe_nas"})
    with pytest.raises(ToolError, match="Unknown or expired"):
        asyncio.run(h.confirm(token))  # single use


def test_confirmation_expires(fake_ha, allowlist_file):
    h = _ha(fake_ha, allowlist_file)
    h.confirm_ttl_s = -1
    token = asyncio.run(h.control(["nas"], "turn_off", None))["confirmation"]["confirmation_token"]
    with pytest.raises(ToolError, match="Unknown or expired"):
        asyncio.run(h.confirm(token))
    assert fake_ha.service_calls == []


def test_ha_errors_are_tool_errors(fake_ha, allowlist_file):
    fake_ha.status = 401
    with pytest.raises(ToolError, match="rejected the access token"):
        asyncio.run(_ha(fake_ha, allowlist_file).list_devices())


def test_config():
    assert HomeAssistantConfig.from_env({}) is None
    assert HomeAssistantConfig.from_env({"HA_URL": "http://h:8123"}) is None  # no token yet -> disabled
    assert HomeAssistantConfig.from_env({"HA_URL": "http://h:8123", "HA_TOKEN": " "}) is None
    with pytest.raises(ValueError):
        HomeAssistantConfig.from_env({"HA_URL": "h:8123", "HA_TOKEN": "t"})
    cfg = HomeAssistantConfig.from_env({"HA_URL": "http://h:8123/", "HA_TOKEN": "t"})
    assert cfg.url == "http://h:8123" and cfg.allowlist_file == "/etc/familia/ha_allowlist.json"


# ----------------------------------------------------------- MCP round trip
def test_mcp_tools_and_internal_marker(fake_ha, allowlist_file, tmp_path):
    env = {
        "FAMILIA_MCP_ENV_FILE": str(tmp_path / "none.env"),
        "HA_URL": "http://homeassistant.local:8123",
        "HA_TOKEN": TOKEN,
        "HA_ALLOWLIST_FILE": str(allowlist_file),
    }
    server = build_server(env, homeassistant_transport=httpx.MockTransport(fake_ha.handler))

    async def go():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            res = await client.call_tool("ha_control", {"devices": ["luz cocina"], "action": "turn_on"})
            bad = await client.call_tool("ha_control", {"devices": ["luz cocina"], "action": "unlock"})
            return tools, res, bad

    tools, res, bad = asyncio.run(go())
    assert set(tools) == {"ha_list_devices", "ha_device_state", "ha_control", "ha_device_history", "ha_confirm_action"}
    assert (tools["ha_confirm_action"].meta or {}).get("familia/internal") is True
    assert not (tools["ha_control"].meta or {}).get("familia/internal")
    assert tools["ha_control"].input_schema["properties"]["action"]["enum"] == ["turn_on", "turn_off", "toggle"]
    assert structured(res)["results"][0]["status"] == "done"
    assert bad.is_error
