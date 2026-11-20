import asyncio
from datetime import date, timedelta

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from conftest import structured
from familia_mcp import health as hl
from familia_mcp.config import HealthConfig
from familia_mcp.server import build_server

KEY = "ow_test_key"
UID = "11111111-2222-3333-4444-555555555555"
UID2 = "99999999-8888-7777-6666-555555555555"
TODAY = date(2026, 11, 20)


def d(n: int) -> str:
    """Date n days before TODAY."""
    return (TODAY - timedelta(days=n)).isoformat()


def ts(n: int, hhmm: str = "00:00", offset: str = "+01:00") -> str:
    return f"{d(n)}T{hhmm}:00{offset}"


SRC = {"provider": "apple", "source": "Health", "device": "iPhone"}


def activity_rows():
    rows = [{"date": d(n), "source": SRC, "steps": 8000 + 100 * n, "distance_meters": 6000.0,
             "active_calories_kcal": 300.0, "active_minutes": 40,
             "intensity_minutes": {"vigorous": 5}} for n in range(0, 14)]
    rows.append({"date": d(3), "source": {"provider": "apple", "source": "Watch"}, "steps": 100})  # duplicate day
    return rows


SLEEP = [
    {"date": d(1), "source": SRC, "start_time": ts(2, "23:30"), "end_time": ts(1, "07:00"),
     "duration_minutes": 420, "time_in_bed_minutes": 450, "efficiency_percent": 93.3,
     "stages": {"deep_minutes": 80, "rem_minutes": 90, "light_minutes": 250, "awake_minutes": 10},
     "interruptions_count": 1, "nap_count": 0},
    {"date": d(0), "source": SRC, "start_time": f"{d(0)}T00:30:00+01:00", "end_time": ts(0, "06:30"),
     "duration_minutes": 330, "stages": None},
]

WEIGHT = {0: 80.0, 6: 80.6, 29: 82.0, 60: 83.5, 89: 84.0}


def series(types, start, end):
    out = []
    if "weight" in types:
        out += [{"timestamp": f"{d(n)}T00:00:00+01:00", "type": "weight", "value": v, "unit": "kg"}
                for n, v in WEIGHT.items()]
    if "body_fat_percentage" in types:
        out.append({"timestamp": f"{d(6)}T00:00:00+01:00", "type": "body_fat_percentage", "value": 21.4,
                    "unit": "percent"})
    if "resting_heart_rate" in types:
        out += [{"timestamp": f"{d(n)}T00:00:00+01:00", "type": "resting_heart_rate", "value": 55 + (n % 3),
                 "unit": "bpm"} for n in range(0, 60)]
    if "heart_rate_variability_sdnn" in types:
        out += [{"timestamp": f"{d(n)}T00:00:00+01:00", "type": "heart_rate_variability_sdnn",
                 "value": 40.0 + n / 10, "unit": "ms"} for n in range(0, 60, 2)]
    return [r for r in out if start <= r["timestamp"][:10] <= end]


WORKOUTS = [
    {"id": "w1", "type": "running", "name": "Carrera", "start_time": f"{d(2)}T18:00:00Z",
     "end_time": f"{d(2)}T18:45:00Z", "duration_seconds": 2700, "source": SRC, "distance_meters": 7500.0,
     "calories_kcal": 520.0, "avg_heart_rate_bpm": 152, "avg_pace_sec_per_km": 360},
    {"id": "w2", "type": "walking", "start_time": f"{d(5)}T09:00:00Z", "end_time": f"{d(5)}T10:00:00Z",
     "duration_seconds": 3600, "source": SRC, "distance_meters": 5000.0},
    {"id": "w3", "type": "running", "start_time": f"{d(9)}T08:00:00Z", "end_time": f"{d(9)}T08:30:00Z",
     "duration_seconds": 1800, "source": SRC, "distance_meters": 5000.0},
]


class FakeOW:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.users = [{"id": UID, "email": "owner@example.com"}]
        self.paginate = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET"
        assert request.headers["X-Open-Wearables-API-Key"] == KEY
        if self.status != 200:
            return httpx.Response(self.status)
        path = request.url.path
        q = request.url.params
        if path == "/api/v1/users":
            users = self.users
            if "email" in q:
                users = [u for u in users if u["email"] == q["email"]]
            return httpx.Response(200, json={"items": users, "total": len(users), "page": 1, "limit": 2})
        prefix = f"/api/v1/users/{UID}"
        if not path.startswith(prefix) and not path.startswith(f"/api/v1/users/{UID2}"):
            return httpx.Response(404)
        sub = path.split("/", 5)[5]

        def page(rows):
            if self.paginate and "cursor" not in q and len(rows) > 1:
                half = len(rows) // 2
                return httpx.Response(200, json={"data": rows[:half], "metadata": {},
                                                 "pagination": {"has_more": True, "next_cursor": "c2"}})
            if self.paginate and q.get("cursor") == "c2":
                rows = rows[len(rows) // 2:]
            return httpx.Response(200, json={"data": rows, "metadata": {},
                                             "pagination": {"has_more": False, "next_cursor": None}})

        def in_range(rows, key="date"):
            return [r for r in rows if q["start_date"] <= r[key][:10] <= q["end_date"]]

        if sub == "summaries/activity":
            return page(in_range(activity_rows()))
        if sub == "summaries/sleep":
            return page(in_range(SLEEP))
        if sub == "summaries/body":
            return httpx.Response(200, json={"source": SRC, "slow_changing": {"bmi": 24.7, "height_cm": 180.0},
                                             "averaged": {}, "latest": {}})
        if sub == "events/workouts":
            rows = in_range(WORKOUTS, "start_time")
            if "record_type" in q:
                rows = [w for w in rows if q["record_type"] in w["type"]]
            return page(rows)
        if sub == "timeseries":
            assert q["resolution"] == "1day"
            return page(series(q.get_list("types"), q["start_time"][:10], q["end_time"][:10]))
        return httpx.Response(404)


@pytest.fixture
def fake_ow():
    return FakeOW()


def make(fake, people=None):
    cfg = HealthConfig(url="https://ow.example:8443", api_key=KEY, people=people or {}, timezone="Europe/Madrid")
    return hl.Health(hl.OpenWearablesClient(cfg, transport=httpx.MockTransport(fake.handler)), cfg,
                     today=lambda: TODAY)


@pytest.fixture
def health(fake_ow):
    return make(fake_ow)


def run(coro):
    return asyncio.run(coro)


def test_activity_week_daily_rows_totals_and_previous_period(health, fake_ow):
    out = run(health.activity(None, "7d", None, None))
    assert out["start"] == d(6) and out["end"] == d(0) and out["days_with_data"] == 7
    assert [r["date"] for r in out["days"]] == [d(n) for n in range(6, -1, -1)]
    day3 = next(r for r in out["days"] if r["date"] == d(3))
    assert day3["steps"] == 8300  # the fuller duplicate wins
    assert out["total_steps"] == sum(8000 + 100 * n for n in range(7))
    assert out["avg_steps_per_day"] == 8300
    assert out["previous_period_avg_steps_per_day"] == 9000  # days 7..13
    assert out["best_day"] == {"date": d(6), "steps": 8600}
    assert out["days"][0]["distance_km"] == 6.0 and out["days"][0]["vigorous_minutes"] == 5
    # Only GETs, only allow-listed paths, and the right window.
    act = [r for r in fake_ow.requests if r.url.path.endswith("summaries/activity")]
    assert act[0].url.params["start_date"] == d(6) and act[0].url.params["end_date"] == d(0)


def test_long_periods_return_weekly_averages(health):
    out = run(health.activity(None, "90d", None, None))
    assert "days" not in out and out["weeks"][0]["week_of"] <= d(13)
    assert all(set(w) >= {"week_of", "days_with_data", "avg_steps"} for w in out["weeks"])


def test_explicit_range_validation(health):
    out = run(health.activity(None, "7d", d(10), d(8)))
    assert out["start"] == d(10) and out["end"] == d(8) and out["days_with_data"] == 3
    with pytest.raises(ToolError, match="after"):
        run(health.activity(None, "7d", d(1), d(5)))
    with pytest.raises(ToolError, match="92 days"):
        run(health.activity(None, "7d", d(200), d(0)))
    with pytest.raises(ToolError, match="YYYY-MM-DD"):
        run(health.activity(None, "7d", "ayer", None))
    future = (TODAY + timedelta(days=5)).isoformat()
    assert run(health.activity(None, "7d", d(2), future))["end"] == d(0)  # clamped to today


def test_sleep_nights_and_averages(health):
    out = run(health.sleep(None, "7d", None, None))
    assert out["nights_with_data"] == 2
    first = out["nights"][0]
    assert first == {"date": d(1), "bedtime": "23:30", "wake_time": "07:00", "asleep_hours": 7.0,
                     "in_bed_hours": 7.5, "efficiency_pct": 93, "deep_min": 80, "rem_min": 90,
                     "light_min": 250, "awake_min": 10, "awakenings": 1}
    assert out["avg_bedtime"] == "00:00"  # 23:30 and 00:30 average across midnight
    assert out["avg_asleep_hours"] == 6.2 and out["shortest_night"] == {"date": d(0), "asleep_hours": 5.5}


def test_body_weight_trend(health, fake_ow):
    out = run(health.body(None, "30d", None, None))
    assert out["latest_weight"] == {"kg": 80.0, "date": d(0)}
    # Closest weigh-in to each target date: 6, 29 and 89 days ago.
    assert out["change_kg"] == {"7d": -0.6, "30d": -2.0, "90d": -4.0}
    assert out["period"]["weigh_in_days"] == 3
    assert out["period"]["first"] == {"kg": 82.0, "date": d(29)}
    assert out["period"]["max"]["kg"] == 82.0 and out["period"]["change_kg"] == -2.0
    assert [w["kg"] for w in out["weigh_ins"]] == [82.0, 80.6, 80.0]
    assert out["latest_body_fat_pct"] == {"value": 21.4, "date": d(6)}
    assert out["bmi"] == 24.7 and out["height_cm"] == 180
    ts_req = next(r for r in fake_ow.requests if r.url.path.endswith("timeseries"))
    assert ts_req.url.params.get_list("types") == ["weight", "body_fat_percentage", "lean_body_mass"]
    assert ts_req.url.params["start_time"] == f"{d(104)}T00:00:00+02:00"  # 90 + 14-day look-back, local midnight (summer time)


def test_body_without_weight(fake_ow, monkeypatch):
    monkeypatch.setattr(__import__(__name__), "WEIGHT", {})
    out = run(make(fake_ow).body(None, "30d", None, None))
    assert "latest_weight" not in out and "No weight" in out["note"]


def test_recovery_current_vs_previous(health):
    out = run(health.recovery(None, "30d", None, None))
    rhr = out["resting_heart_rate"]
    assert rhr["unit"] == "bpm" and rhr["latest"] == {"value": 55, "date": d(0)}
    assert rhr["days_with_data"] == 30 and rhr["min"] == 55 and rhr["max"] == 57
    assert rhr["previous_period_avg"] is not None
    hrv = out["hrv_sdnn"]
    assert hrv["latest"]["value"] == 40 and hrv["days_with_data"] == 15
    assert "hrv_rmssd" not in out


def test_workouts_newest_first_with_totals_and_filter(health, fake_ow):
    out = run(health.workouts(None, "30d", None, None, None))
    assert out["count"] == 3 and [w["type"] for w in out["recent"]] == ["running", "walking", "running"]
    first = out["recent"][0]
    assert first["start"] == f"{d(2)}T19:00+01:00"  # local time
    assert first["duration_min"] == 45 and first["distance_km"] == 7.5 and first["pace_min_per_km"] == "6:00"
    assert out["by_type"]["running"] == {"count": 2, "minutes": 75, "km": 12.5}
    assert out["total_minutes"] == 135
    run_only = run(health.workouts(None, "30d", None, None, "running"))
    assert run_only["count"] == 2 and run_only["type_filter"] == "running"
    assert fake_ow.requests[-1].url.params["record_type"] == "running"


def test_pagination_is_followed(health, fake_ow):
    fake_ow.paginate = True
    out = run(health.workouts(None, "30d", None, None, None))
    assert out["count"] == 3
    assert any(r.url.params.get("cursor") == "c2" for r in fake_ow.requests)


def test_people_mapping_by_email(fake_ow):
    fake_ow.users = [{"id": UID, "email": "owner@example.com"}, {"id": UID2, "email": "ana@example.com"}]
    h = make(fake_ow, {"Manuel": "owner@example.com", "Ana": "ana@example.com"})
    assert run(h.activity(None, "7d", None, None))["person"] == "Manuel"
    out = run(h.workouts("ana", "30d", None, None, None))
    assert out["person"] == "Ana"
    assert UID2 in fake_ow.requests[-1].url.path
    with pytest.raises(ToolError):
        run(h.activity("abuela", "7d", None, None))


def test_single_user_required_without_mapping(fake_ow):
    fake_ow.users = [{"id": UID, "email": "a@x"}, {"id": UID2, "email": "b@x"}]
    with pytest.raises(ToolError, match="OPENWEARABLES_USERS"):
        run(make(fake_ow).activity(None, "7d", None, None))
    with pytest.raises(ToolError, match="Only one person"):
        run(make(FakeOW()).activity("ana", "7d", None, None))


def test_errors_are_tool_errors(health, fake_ow):
    fake_ow.status = 401
    with pytest.raises(ToolError, match="API key"):
        run(health.activity(None, "7d", None, None))


def test_client_refuses_paths_outside_allowlist(health):
    with pytest.raises(ToolError, match="not allowed"):
        run(health.client.get("/api/v1/users/11111111-2222-3333-4444-555555555555/connections"))
    with pytest.raises(ToolError, match="not allowed"):
        run(health.client.get("/api/v1/users/../admin/summaries/activity"))


def test_config_from_env():
    assert HealthConfig.from_env({}) is None
    cfg = HealthConfig.from_env({"OPENWEARABLES_URL": "https://ow:8443/", "OPENWEARABLES_API_KEY": "k",
                                 "OPENWEARABLES_USERS": "Manuel=m@x.es;Ana=a@x.es",
                                 "OPENWEARABLES_TIMEZONE": "Europe/Madrid"})
    assert cfg.url == "https://ow:8443" and cfg.people == {"Manuel": "m@x.es", "Ana": "a@x.es"}
    with pytest.raises(ValueError):
        HealthConfig.from_env({"OPENWEARABLES_URL": "ow:8443", "OPENWEARABLES_API_KEY": "k"})
    with pytest.raises(ValueError, match="Name=email"):
        HealthConfig.from_env({"OPENWEARABLES_URL": "https://ow", "OPENWEARABLES_API_KEY": "k",
                               "OPENWEARABLES_USERS": "Manuel=notanemail"})


def test_tools_registered_read_only_through_server(fake_ow, tmp_path):
    env = {"FAMILIA_MCP_ENV_FILE": str(tmp_path / "missing.env"),
           "OPENWEARABLES_URL": "https://ow.example:8443", "OPENWEARABLES_API_KEY": KEY,
           "OPENWEARABLES_TIMEZONE": "Europe/Madrid"}
    server = build_server(env, health_transport=httpx.MockTransport(fake_ow.handler), health_today=lambda: TODAY)

    async def go():
        async with Client(server) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert {"health_activity", "health_sleep", "health_body", "health_workouts",
                    "health_recovery"} <= set(tools)
            assert all(tools[n].annotations.read_only_hint for n in tools if n.startswith("health_"))
            r = await c.call_tool("health_body", {"period": "7d"})
            assert not r.is_error
            return structured(r)

    out = asyncio.run(go())
    assert out["latest_weight"]["kg"] == 80.0
