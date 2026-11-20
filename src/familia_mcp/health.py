"""Health module: read-only summaries of the household's wearable data.

What the household asks by voice: "¿cuánto he andado esta semana?",
"¿cómo he dormido?", "¿cómo va mi peso?", "¿qué entrenos hice este mes?",
"¿qué tal mi pulso en reposo?".

Source: a self-hosted Open Wearables instance (REST API, header
X-Open-Wearables-API-Key). Its API keys have no scopes and can also write,
so the client only issues GET requests to the fixed paths in
`_ALLOWED_PATHS`: that check is the boundary.

Data minimisation: tool results go to the language model, so every tool
returns summaries (daily rows up to a month, weekly averages beyond, totals,
trends), never raw samples such as minute-by-minute heart rate.

Open Wearables' recovery summary only has WHOOP data, and its body summary
only the latest weight, so resting heart rate, HRV and the weight trend are
read from the daily time series (resolution=1day).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from statistics import mean
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .config import HealthConfig
from .matching import resolve

logger = logging.getLogger("familia_mcp.health")

_UUID = r"[0-9a-fA-F-]{36}"
# The only requests this module can make (GET only).
_ALLOWED_PATHS = (
    re.compile(r"^/api/v1/users$"),
    re.compile(rf"^/api/v1/users/{_UUID}/summaries/(activity|sleep|body)$"),
    re.compile(rf"^/api/v1/users/{_UUID}/events/workouts$"),
    re.compile(rf"^/api/v1/users/{_UUID}/timeseries$"),
)

PERIODS = {"7d": 7, "30d": 30, "90d": 90}
MAX_RANGE_DAYS = 92
DAILY_ROWS_MAX = 31          # longer periods are returned as weekly averages
MAX_WORKOUTS_LISTED = 25
MAX_PAGES = 5                # pagination guard (1000 rows per page)
PAGE_SIZE = 1000
CHANGE_TOLERANCE_BEFORE = 14  # weight change: reference weigh-in window around the target date
CHANGE_TOLERANCE_AFTER = 7

Period = Literal["7d", "30d", "90d"]


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
class OpenWearablesClient:
    def __init__(self, cfg: HealthConfig, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=cfg.url,
            timeout=cfg.timeout_s,
            headers={"Accept": "application/json", "X-Open-Wearables-API-Key": cfg.api_key},
            transport=transport,
        )

    async def get(self, path: str, params: dict[str, Any] | list[tuple[str, Any]] | None = None) -> Any:
        if not any(p.match(path) for p in _ALLOWED_PATHS):
            raise ToolError(f"Open Wearables path not allowed: {path}")  # programming error, never user input
        try:
            resp = await self._http.get(path, params=params)
        except httpx.HTTPError as exc:
            raise ToolError(f"Open Wearables is not reachable ({type(exc).__name__}).") from exc
        if resp.status_code in (401, 403):
            raise ToolError("Open Wearables rejected the API key.")
        if resp.status_code == 404:
            raise ToolError("Open Wearables user not found.")
        if resp.status_code >= 400:
            raise ToolError(f"Open Wearables returned HTTP {resp.status_code}.")
        try:
            return resp.json()
        except ValueError as exc:
            raise ToolError("Open Wearables returned a non-JSON response.") from exc

    async def get_all(self, path: str, params: list[tuple[str, Any]]) -> list[dict[str, Any]]:
        """Follow cursor pagination (PaginatedResponse: data + pagination)."""
        rows: list[dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_PAGES):
            page = await self.get(path, params + ([("cursor", cursor)] if cursor else []))
            data = page.get("data") if isinstance(page, dict) else None
            rows.extend(r for r in data or [] if isinstance(r, dict))
            pag = page.get("pagination") or {} if isinstance(page, dict) else {}
            cursor = pag.get("next_cursor")
            if not pag.get("has_more") or not cursor:
                break
        return rows


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _num(v: Any, digits: int = 1) -> float | None:
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _avg(values: list[float | int | None], digits: int = 1) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return round(mean(vals), digits) if vals else None


def _parse_dt(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_date(raw: Any) -> date | None:
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    return None


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _clean(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None and v != [] and v != {}}


def _hhmm_from_minutes(m: float) -> str:
    m = int(round(m)) % 1440
    return f"{m // 60:02d}:{m % 60:02d}"


# --------------------------------------------------------------------------
# Domain logic
# --------------------------------------------------------------------------
class Health:
    def __init__(self, client: OpenWearablesClient, cfg: HealthConfig, today: Any = None):
        self.client = client
        self.cfg = cfg
        self.tz = ZoneInfo(cfg.timezone)
        self._today = today  # injectable for tests: callable -> date
        self._ids: dict[str, str] = {}

    # ---- dates -----------------------------------------------------------
    def today(self) -> date:
        return self._today() if self._today else datetime.now(self.tz).date()

    def window(self, period: str, start: str | None, end: str | None) -> tuple[date, date]:
        """Inclusive local-date window ending today, or an explicit range."""
        today = self.today()
        if start or end:
            try:
                s = date.fromisoformat(start) if start else None
                e = date.fromisoformat(end) if end else today
            except ValueError as exc:
                raise ToolError("Dates must look like YYYY-MM-DD.") from exc
            e = min(e, today)
            s = s or e - timedelta(days=PERIODS.get(period, 7) - 1)
            if s > e:
                raise ToolError("start_date is after end_date.")
            if (e - s).days + 1 > MAX_RANGE_DAYS:
                raise ToolError(f"The range is limited to {MAX_RANGE_DAYS} days.")
            return s, e
        if period not in PERIODS:
            raise ToolError(f"Unknown period '{period}'.")
        return today - timedelta(days=PERIODS[period] - 1), today

    def _local_midnight(self, d: date) -> str:
        return datetime.combine(d, time.min, self.tz).isoformat()

    def _local(self, raw: Any) -> datetime | None:
        dt = _parse_dt(raw)
        return dt.astimezone(self.tz) if dt else None

    # ---- users -----------------------------------------------------------
    async def _user(self, person: str | None) -> tuple[str, str]:
        """-> (Open Wearables user id, display name)."""
        people = self.cfg.people
        if people:
            items = list(people.items())
            name, email = items[0] if not person else resolve(
                person, items, names=lambda i: [i[0]], label=lambda i: i[0], kind="person")
            if email not in self._ids:
                page = await self.client.get("/api/v1/users", {"email": email, "limit": 2})
                found = [u for u in (page or {}).get("items") or [] if isinstance(u, dict) and u.get("id")]
                if not found:
                    raise ToolError(f"No Open Wearables user for {name}.")
                self._ids[email] = str(found[0]["id"])
            return self._ids[email], name
        if person:
            raise ToolError("Only one person is configured for health data.")
        if "_single" not in self._ids:
            page = await self.client.get("/api/v1/users", {"limit": 2})
            found = [u for u in (page or {}).get("items") or [] if isinstance(u, dict) and u.get("id")]
            if len(found) != 1:
                raise ToolError("Set OPENWEARABLES_USERS: the instance has more than one user (or none).")
            self._ids["_single"] = str(found[0]["id"])
        return self._ids["_single"], "owner"

    # ---- activity --------------------------------------------------------
    async def _activity_rows(self, uid: str, s: date, e: date) -> dict[date, dict[str, Any]]:
        rows = await self.client.get_all(
            f"/api/v1/users/{uid}/summaries/activity",
            [("start_date", s.isoformat()), ("end_date", e.isoformat()), ("limit", PAGE_SIZE)])
        by_day: dict[date, dict[str, Any]] = {}
        for r in rows:
            d = _parse_date(r.get("date"))
            if d is None or not (s <= d <= e):
                continue
            # One row per date is expected; if a source duplicates a day keep the fullest one.
            if d not in by_day or (r.get("steps") or 0) > (by_day[d].get("steps") or 0):
                by_day[d] = r
        return by_day

    async def activity(self, person: str | None, period: str, start: str | None, end: str | None) -> dict[str, Any]:
        uid, name = await self._user(person)
        s, e = self.window(period, start, end)
        days = await self._activity_rows(uid, s, e)
        span = (e - s).days + 1
        prev = await self._activity_rows(uid, s - timedelta(days=span), s - timedelta(days=1))

        def row(d: date, r: dict[str, Any]) -> dict[str, Any]:
            intensity = r.get("intensity_minutes") or {}
            return _clean({
                "date": d.isoformat(),
                "steps": _int(r.get("steps")),
                "distance_km": _num((r.get("distance_meters") or 0) / 1000, 2) if r.get("distance_meters") else None,
                "active_kcal": _int(r.get("active_calories_kcal")),
                "active_minutes": _int(r.get("active_minutes")),
                "floors": _int(r.get("floors_climbed")),
                "vigorous_minutes": _int(intensity.get("vigorous")) if isinstance(intensity, dict) else None,
            })

        rows = [row(d, days[d]) for d in sorted(days)]
        steps = [r["steps"] for r in rows if "steps" in r]
        prev_steps = [_int(r.get("steps")) for r in prev.values() if r.get("steps") is not None]
        out: dict[str, Any] = {
            "person": name, "start": s.isoformat(), "end": e.isoformat(), "days_with_data": len(rows),
            "total_steps": sum(steps) if steps else None,
            "avg_steps_per_day": _int(mean(steps)) if steps else None,
            "total_distance_km": _num(sum(r.get("distance_km", 0) for r in rows), 1) if rows else None,
            "avg_active_kcal_per_day": _avg([r.get("active_kcal") for r in rows], 0),
            "avg_active_minutes_per_day": _avg([r.get("active_minutes") for r in rows], 0),
            "previous_period_avg_steps_per_day": _int(mean(prev_steps)) if prev_steps else None,
        }
        if steps:
            best = max(rows, key=lambda r: r.get("steps", -1))
            out["best_day"] = {"date": best["date"], "steps": best["steps"]}
        if span <= DAILY_ROWS_MAX:
            out["days"] = rows
        else:
            out["weeks"] = _weekly(rows, {"steps": "avg_steps", "distance_km": "avg_distance_km",
                                          "active_minutes": "avg_active_minutes"})
        return _clean(out)

    # ---- sleep -----------------------------------------------------------
    async def sleep(self, person: str | None, period: str, start: str | None, end: str | None) -> dict[str, Any]:
        uid, name = await self._user(person)
        s, e = self.window(period, start, end)
        rows = await self.client.get_all(
            f"/api/v1/users/{uid}/summaries/sleep",
            [("start_date", s.isoformat()), ("end_date", e.isoformat()), ("limit", PAGE_SIZE)])
        nights: dict[date, dict[str, Any]] = {}
        bed_m, wake_m = [], []
        for r in rows:
            d = _parse_date(r.get("date"))
            if d is None or not (s <= d <= e):
                continue
            st, en = self._local(r.get("start_time")), self._local(r.get("end_time"))
            stages = r.get("stages") or {}
            night = _clean({
                "date": d.isoformat(),
                "bedtime": st.strftime("%H:%M") if st else None,
                "wake_time": en.strftime("%H:%M") if en else None,
                "asleep_hours": _num((r.get("duration_minutes") or 0) / 60, 1) if r.get("duration_minutes") else None,
                "in_bed_hours": _num((r.get("time_in_bed_minutes") or 0) / 60, 1) if r.get("time_in_bed_minutes") else None,
                "efficiency_pct": _int(r.get("efficiency_percent")),
                "deep_min": _int(stages.get("deep_minutes")) if isinstance(stages, dict) else None,
                "rem_min": _int(stages.get("rem_minutes")) if isinstance(stages, dict) else None,
                "light_min": _int(stages.get("light_minutes")) if isinstance(stages, dict) else None,
                "awake_min": _int(stages.get("awake_minutes")) if isinstance(stages, dict) else None,
                "awakenings": _int(r.get("interruptions_count")),
                "naps_min": _int(r.get("nap_duration_minutes")) if r.get("nap_count") else None,
            })
            if d in nights and (nights[d].get("asleep_hours") or 0) >= (night.get("asleep_hours") or 0):
                continue
            nights[d] = night
        for n in nights.values():
            if "bedtime" in n:
                h, m = map(int, n["bedtime"].split(":"))
                bed_m.append((h * 60 + m - 720) % 1440)  # minutes after noon: 23:30 and 00:30 average well
            if "wake_time" in n:
                h, m = map(int, n["wake_time"].split(":"))
                wake_m.append(h * 60 + m)
        ordered = [nights[d] for d in sorted(nights)]
        span = (e - s).days + 1
        out: dict[str, Any] = {
            "person": name, "start": s.isoformat(), "end": e.isoformat(), "nights_with_data": len(ordered),
            "avg_asleep_hours": _avg([n.get("asleep_hours") for n in ordered], 1),
            "avg_bedtime": _hhmm_from_minutes(mean(bed_m) + 720) if bed_m else None,
            "avg_wake_time": _hhmm_from_minutes(mean(wake_m)) if wake_m else None,
            "avg_deep_min": _avg([n.get("deep_min") for n in ordered], 0),
            "avg_rem_min": _avg([n.get("rem_min") for n in ordered], 0),
            "avg_efficiency_pct": _avg([n.get("efficiency_pct") for n in ordered], 0),
        }
        if ordered:
            shortest = min((n for n in ordered if "asleep_hours" in n), key=lambda n: n["asleep_hours"], default=None)
            if shortest:
                out["shortest_night"] = {"date": shortest["date"], "asleep_hours": shortest["asleep_hours"]}
        if span <= DAILY_ROWS_MAX:
            out["nights"] = ordered
        else:
            out["weeks"] = _weekly(ordered, {"asleep_hours": "avg_asleep_hours", "deep_min": "avg_deep_min"})
        return _clean(out)

    # ---- time series (daily) --------------------------------------------
    async def _daily_series(self, uid: str, types: list[str], s: date, e: date) -> dict[str, dict[date, float]]:
        params: list[tuple[str, Any]] = [("start_time", self._local_midnight(s)),
                                         ("end_time", self._local_midnight(e + timedelta(days=1))),
                                         ("resolution", "1day"), ("limit", PAGE_SIZE)]
        params += [("types", t) for t in types]
        rows = await self.client.get_all(f"/api/v1/users/{uid}/timeseries", params)
        out: dict[str, dict[date, float]] = {t: {} for t in types}
        for r in rows:
            t = r.get("type")
            ts = self._local(r.get("timestamp"))
            v = _num(r.get("value"), 2)
            if t not in out or ts is None or v is None:
                continue
            d = ts.date()
            if s <= d <= e:
                out[t][d] = v
        return out

    async def body(self, person: str | None, period: str, start: str | None, end: str | None) -> dict[str, Any]:
        uid, name = await self._user(person)
        s, e = self.window(period, start, end)
        # Look back further than the window so "change in 30/90 days" and the
        # latest weight work even when the window has few weigh-ins.
        lookback = min(s, e - timedelta(days=90 + CHANGE_TOLERANCE_BEFORE))
        series = await self._daily_series(uid, ["weight", "body_fat_percentage", "lean_body_mass"], lookback, e)
        weight = series["weight"]
        out: dict[str, Any] = {"person": name, "start": s.isoformat(), "end": e.isoformat()}
        if weight:
            last_d = max(weight)
            last = weight[last_d]
            out["latest_weight"] = {"kg": round(last, 1), "date": last_d.isoformat()}

            def change(days: int) -> float | None:
                # Weigh-ins are irregular: compare with the one closest to the target
                # date (up to 14 days before or 7 after it), not "anything older".
                target = last_d - timedelta(days=days)
                ref = [d for d in weight
                       if target - timedelta(days=CHANGE_TOLERANCE_BEFORE) <= d
                       <= min(target + timedelta(days=CHANGE_TOLERANCE_AFTER), last_d - timedelta(days=1))]
                if not ref:
                    return None
                best = min(ref, key=lambda d: (abs((d - target).days), d))
                return round(last - weight[best], 1)

            out["change_kg"] = _clean({"7d": change(7), "30d": change(30), "90d": change(90)})
            in_win = {d: v for d, v in weight.items() if s <= d <= e}
            if in_win:
                lo, hi = min(in_win, key=in_win.get), max(in_win, key=in_win.get)
                first_d = min(in_win)
                out["period"] = {
                    "weigh_in_days": len(in_win),
                    "first": {"kg": round(in_win[first_d], 1), "date": first_d.isoformat()},
                    "min": {"kg": round(in_win[lo], 1), "date": lo.isoformat()},
                    "max": {"kg": round(in_win[hi], 1), "date": hi.isoformat()},
                    "change_kg": round(in_win[max(in_win)] - in_win[first_d], 1),
                }
                rows = [{"date": d.isoformat(), "kg": round(v, 1)} for d, v in sorted(in_win.items())]
                if (e - s).days + 1 <= DAILY_ROWS_MAX:
                    out["weigh_ins"] = rows
                else:
                    out["weeks"] = _weekly(rows, {"kg": "avg_kg"})
        for key, label in (("body_fat_percentage", "latest_body_fat_pct"), ("lean_body_mass", "latest_lean_mass_kg")):
            vals = series[key]
            if vals:
                d = max(vals)
                out[label] = {"value": round(vals[d], 1), "date": d.isoformat()}
        try:
            summary = await self.client.get(f"/api/v1/users/{uid}/summaries/body")
        except ToolError:
            summary = None  # BMI and height are a nicety
        slow = (summary or {}).get("slow_changing") or {} if isinstance(summary, dict) else {}
        out["bmi"] = _num(slow.get("bmi"), 1)
        out["height_cm"] = _num(slow.get("height_cm"), 0)
        if not weight:
            out["note"] = "No weight measurements in Open Wearables."
        return _clean(out)

    async def recovery(self, person: str | None, period: str, start: str | None, end: str | None) -> dict[str, Any]:
        uid, name = await self._user(person)
        s, e = self.window(period, start, end)
        span = (e - s).days + 1
        prev_s = s - timedelta(days=span)
        types = ["resting_heart_rate", "heart_rate_variability_sdnn", "heart_rate_variability_rmssd"]
        series = await self._daily_series(uid, types, prev_s, e)
        out: dict[str, Any] = {"person": name, "start": s.isoformat(), "end": e.isoformat()}
        labels = {"resting_heart_rate": ("resting_heart_rate", "bpm", 0),
                  "heart_rate_variability_sdnn": ("hrv_sdnn", "ms", 0),
                  "heart_rate_variability_rmssd": ("hrv_rmssd", "ms", 0)}
        for t, (label, unit, digits) in labels.items():
            vals = series[t]
            cur = {d: v for d, v in vals.items() if s <= d}
            prev = [v for d, v in vals.items() if d < s]
            if not cur:
                continue
            last_d = max(cur)
            item: dict[str, Any] = {
                "unit": unit,
                "latest": {"value": _int(cur[last_d]), "date": last_d.isoformat()},
                "avg": _avg(list(cur.values()), digits),
                "min": _int(min(cur.values())),
                "max": _int(max(cur.values())),
                "days_with_data": len(cur),
                "previous_period_avg": _avg(prev, digits),
            }
            if span > DAILY_ROWS_MAX:
                item["weeks"] = _weekly([{"date": d.isoformat(), "v": v} for d, v in sorted(cur.items())], {"v": "avg"})
            out[label] = _clean(item)
        if len(out) == 3:
            out["note"] = "No resting heart rate or HRV data in Open Wearables for this period."
        return _clean(out)

    # ---- workouts --------------------------------------------------------
    async def workouts(self, person: str | None, period: str, start: str | None, end: str | None,
                       kind: str | None) -> dict[str, Any]:
        uid, name = await self._user(person)
        s, e = self.window(period, start, end)
        params: list[tuple[str, Any]] = [("start_date", s.isoformat()), ("end_date", e.isoformat()),
                                         ("limit", PAGE_SIZE)]
        if kind:
            params.append(("record_type", kind))
        rows = await self.client.get_all(f"/api/v1/users/{uid}/events/workouts", params)
        items, by_type = [], {}
        for w in rows:
            st = self._local(w.get("start_time"))
            if st is None or not (s <= st.date() <= e):
                continue
            wtype = str(w.get("type") or "other")
            dur_min = _int((w.get("duration_seconds") or 0) / 60) if w.get("duration_seconds") else None
            km = _num((w.get("distance_meters") or 0) / 1000, 2) if w.get("distance_meters") else None
            pace = w.get("avg_pace_sec_per_km")
            items.append((st, _clean({
                "type": wtype,
                "name": w.get("name"),
                "start": st.isoformat(timespec="minutes"),
                "duration_min": dur_min,
                "distance_km": km,
                "kcal": _int(w.get("calories_kcal")),
                "avg_hr_bpm": _int(w.get("avg_heart_rate_bpm")),
                "max_hr_bpm": _int(w.get("max_heart_rate_bpm")),
                "pace_min_per_km": f"{int(pace) // 60}:{int(pace) % 60:02d}" if _int(pace) else None,
                "elevation_gain_m": _int(w.get("elevation_gain_meters")),
            })))
            t = by_type.setdefault(wtype, {"count": 0, "minutes": 0, "km": 0.0})
            t["count"] += 1
            t["minutes"] += dur_min or 0
            t["km"] = round(t["km"] + (km or 0), 1)
        items.sort(key=lambda x: x[0], reverse=True)
        return _clean({
            "person": name, "start": s.isoformat(), "end": e.isoformat(), "type_filter": kind,
            "count": len(items),
            "total_minutes": sum(t["minutes"] for t in by_type.values()) if items else None,
            "by_type": {k: _clean({**v, "km": v["km"] or None}) for k, v in sorted(by_type.items())},
            "recent": [i for _, i in items[:MAX_WORKOUTS_LISTED]],
        })


def _weekly(rows: list[dict[str, Any]], fields: dict[str, str]) -> list[dict[str, Any]]:
    """Daily rows ({'date': 'YYYY-MM-DD', ...}) -> weekly averages (weeks start on Monday)."""
    weeks: dict[date, list[dict[str, Any]]] = {}
    for r in rows:
        d = _parse_date(r.get("date"))
        if d:
            weeks.setdefault(_week_start(d), []).append(r)
    out = []
    for wk in sorted(weeks):
        group = weeks[wk]
        item = {"week_of": wk.isoformat(), "days_with_data": len(group)}
        for src, dst in fields.items():
            item[dst] = _avg([g.get(src) for g in group], 1)
        out.append(_clean(item))
    return out


# --------------------------------------------------------------------------
# MCP tool registration
# --------------------------------------------------------------------------
_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)

_PERIOD = Field(description="Look-back window ending today (ignored when start_date is given).")
_START = Field(description="Optional range start, YYYY-MM-DD (e.g. last month). Max 92 days.",
               pattern=r"^\d{4}-\d{2}-\d{2}$")
_PERSON = Field(description="Whose data; omit for the owner.", max_length=50)
_END = Field(description="Optional range end, YYYY-MM-DD; defaults to today.", pattern=r"^\d{4}-\d{2}-\d{2}$")


def register(server: MCPServer, health: Health) -> None:
    people = ", ".join(health.cfg.people) or "only the owner"
    person_note = f" Person: omit for the owner; configured: {people}."
    common = (" Data comes from the household's wearables and phones (Apple Health, Health Connect, "
              "Withings...) through Open Wearables. Metric units. Periods up to a month return daily rows, "
              "longer ones weekly averages. Do not give medical advice beyond the numbers.")
    @server.tool(
        name="health_activity",
        description=("Daily activity: steps, distance, active calories and minutes, with totals, daily "
                     "average, best day and the previous period's average for comparison. Use for "
                     "'¿cuánto he andado esta semana?'." + person_note + common),
        annotations=_READ_ONLY,
    )
    async def health_activity(
        person: Annotated[str | None, _PERSON] = None,
        period: Annotated[Period, _PERIOD] = "7d",
        start_date: Annotated[str | None, _START] = None,
        end_date: Annotated[str | None, _END] = None,
    ) -> dict[str, Any]:
        return await health.activity(person, period, start_date, end_date)

    @server.tool(
        name="health_sleep",
        description=("Sleep per night: bedtime, wake time, hours asleep, efficiency and stages (deep, REM, "
                     "light, awake) when the device reports them, with averages and the shortest night. "
                     "Use for '¿cómo he dormido?'." + person_note + common),
        annotations=_READ_ONLY,
    )
    async def health_sleep(
        person: Annotated[str | None, _PERSON] = None,
        period: Annotated[Period, _PERIOD] = "7d",
        start_date: Annotated[str | None, _START] = None,
        end_date: Annotated[str | None, _END] = None,
    ) -> dict[str, Any]:
        return await health.sleep(person, period, start_date, end_date)

    @server.tool(
        name="health_body",
        description=("Weight trend: latest weight and date, change over 7/30/90 days, first/min/max in the "
                     "period and the weigh-ins (weekly averages for long periods); latest body fat, lean "
                     "mass and BMI when available. Use for '¿cómo va mi peso?'." + person_note + common),
        annotations=_READ_ONLY,
    )
    async def health_body(
        person: Annotated[str | None, _PERSON] = None,
        period: Annotated[Period, _PERIOD] = "30d",
        start_date: Annotated[str | None, _START] = None,
        end_date: Annotated[str | None, _END] = None,
    ) -> dict[str, Any]:
        return await health.body(person, period, start_date, end_date)

    @server.tool(
        name="health_workouts",
        description=("Workouts in the period, newest first: type, start, duration, distance, calories, heart "
                     "rate and pace; totals per type. Optional type filter in English (e.g. 'running', "
                     "'walking', 'cycling', 'swimming', 'strength'). Use for '¿qué entrenos hice este mes?'."
                     + person_note + common),
        annotations=_READ_ONLY,
    )
    async def health_workouts(
        person: Annotated[str | None, _PERSON] = None,
        period: Annotated[Period, _PERIOD] = "30d",
        start_date: Annotated[str | None, _START] = None,
        end_date: Annotated[str | None, _END] = None,
        workout_type: Annotated[str | None, Field(description="Workout type filter, English, substring match.",
                                                  max_length=40, pattern=r"^[A-Za-z_ -]+$")] = None,
    ) -> dict[str, Any]:
        return await health.workouts(person, period, start_date, end_date, workout_type)

    @server.tool(
        name="health_recovery",
        description=("Resting heart rate and heart rate variability (HRV, SDNN for Apple Watch, RMSSD for "
                     "other devices): latest value, average, min/max and the previous period's average. Use "
                     "for '¿qué tal mi pulso en reposo?'." + person_note + common),
        annotations=_READ_ONLY,
    )
    async def health_recovery(
        person: Annotated[str | None, _PERSON] = None,
        period: Annotated[Period, _PERIOD] = "30d",
        start_date: Annotated[str | None, _START] = None,
        end_date: Annotated[str | None, _END] = None,
    ) -> dict[str, Any]:
        return await health.recovery(person, period, start_date, end_date)
