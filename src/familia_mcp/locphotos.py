"""Photo evidence for the locations module, from the household PhotoPrism.

Uses the same PhotoPrism MCP connection (and token) as the photos module. The
token needs the `places` scope: get_media then returns a photo's exact
position, which is checked against the same geometry as the GPS history. Photo
coordinates never reach the model; answers carry place names, local times,
counts and people.

- near():   photos taken at a place (was_i_at). PhotoPrism's own radius search
            (lat:/lng:/dist: filters) finds candidates around the place; one
            get_media per checked photo confirms it is inside the place.
            Lookups are capped per day and per call.
- on_day(): photos of one local day (where_was_i), from search results only.
- in_range(): photos of a date range (trip_summary), from search results only.

Days are the photo's local day (PhotoPrism's taken_at_local), like the GPS.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import date, timedelta
from typing import Any, Iterable

from mcp.server.mcpserver.exceptions import ToolError

from .geo import haversine_m
from .matching import resolve
from .photos import CallTool, Connect

PAGE = 50
SCAN_LIMIT = 1000          # max photos scanned for one answer
MAX_LOOKUPS = 40           # max get_media calls for one answer
LOOKUPS_PER_DAY_END = 4    # per day: try this many from the start and from the end
PHOTO_TOLERANCE_M = 50.0   # phone GPS in photo metadata is rarely better than this
MAX_RADIUS_KM = 300.0
TOP = 5


def _local_day(item: dict[str, Any]) -> str:
    return (item.get("taken_at_local") or "")[:10]


def _local_time(item: dict[str, Any]) -> str:
    return (item.get("taken_at_local") or "")[11:16]


def _place_name(item: dict[str, Any]) -> str | None:
    place = item.get("place")
    name = place.get("name") if isinstance(place, dict) else None
    if not name:
        return None
    parts = [p.strip() for p in name.split(",") if p.strip()]
    return ", ".join(parts[:2])  # "Níjar, Andalucía, Spain" -> "Níjar, Andalucía"


def _people(items: Iterable[dict[str, Any]]) -> list[str]:
    c = Counter(p.get("name") for i in items for p in (i.get("people") or []) if isinstance(p, dict) and p.get("name"))
    return [n for n, _ in c.most_common(8)]


def _date_args(s: date | None, e: date | None) -> dict[str, str]:
    """PhotoPrism filters on its own day boundaries: widen by a day on each
    side and filter by local day afterwards (trips across time zones)."""
    args = {}
    if s:
        args["taken_after"] = (s - timedelta(days=1)).isoformat()
    if e:
        args["taken_before"] = (e + timedelta(days=2)).isoformat()
    return args


def _in_range(day: str, s: date | None, e: date | None) -> bool:
    return bool(day) and (not s or day >= s.isoformat()) and (not e or day <= e.isoformat())


class PhotoEvidence:
    def __init__(self, connect: Connect, now=time.monotonic):
        self.connect = connect
        self._now = now

    # ------------------------------------------------ helpers

    async def _scan(self, call: CallTool, args: dict[str, Any], deadline: float) -> tuple[list[dict[str, Any]], bool]:
        """All media for a search (oldest first), up to SCAN_LIMIT -> (items, more_exist)."""
        items: list[dict[str, Any]] = []
        offset = 0
        while len(items) < SCAN_LIMIT:
            if deadline - self._now() < 1.0:
                return items, True
            page = await call("search_media", {**args, "order": "oldest", "detail": "full",
                                               "limit": PAGE, "offset": offset})
            batch = [i for i in page.get("items") or [] if isinstance(i, dict)]
            items.extend(batch)
            nxt = page.get("next_offset")
            if not batch or nxt is None:
                return items, False
            offset = int(nxt)
        return items[:SCAN_LIMIT], True

    async def _person_uid(self, call: CallTool, name: str) -> tuple[str, str]:
        people, offset = [], 0
        while True:
            page = await call("list_people", {"limit": PAGE, "offset": offset})
            batch = [p for p in page.get("items") or [] if isinstance(p, dict) and p.get("uid")]
            people.extend(batch)
            if not page.get("truncated") or not batch:
                break
            offset += len(batch)
        p = resolve(name, people, names=lambda x: [x.get("name") or ""], label=lambda x: x.get("name") or "?",
                    kind="person")
        return p["uid"], p.get("name") or name

    @staticmethod
    def _summary(items: list[dict[str, Any]], one_day: bool) -> dict[str, Any]:
        times = sorted(_local_time(i) for i in items if _local_time(i))
        places = Counter(n for i in items if (n := _place_name(i)))
        out: dict[str, Any] = {"count": len(items)}
        if times and one_day:  # across several days a first/last clock time means nothing
            out["first_time"], out["last_time"] = times[0], times[-1]
        if places:
            out["places"] = [p for p, _ in places.most_common(TOP)]
        people = _people(items)
        if people:
            out["people"] = people
        return out

    # ------------------------------------------------ queries

    async def near(self, place: Any, s: date | None, e: date | None, person: str | None,
                   deadline: float) -> dict[str, Any]:
        """Photos inside `place` (a phonetrack.Place) in the period, per local day."""
        box = place.shape.bbox().expand(place.tolerance_m + PHOTO_TOLERANCE_M)
        lon, lat = box.center
        radius_km = min(MAX_RADIUS_KM, max(0.1, haversine_m((lon, lat), (box.max_lon, box.max_lat)) / 1000))
        out: dict[str, Any] = {}
        async with self.connect() as call:
            args: dict[str, Any] = {"query": f"lat:{lat:.6f} lng:{lon:.6f} dist:{radius_km:.3f}", **_date_args(s, e)}
            if person:
                uid, out["person"] = await self._person_uid(call, person)
                args["person"] = uid
            items, more = await self._scan(call, args, deadline)
            by_day: dict[str, list[dict[str, Any]]] = {}
            for i in items:
                if _in_range(d := _local_day(i), s, e):
                    by_day.setdefault(d, []).append(i)
            lookups = 0
            located: dict[str, bool] = {}

            async def inside(item: dict[str, Any]) -> bool | None:
                nonlocal lookups
                uid = item.get("uid")
                if uid in located:
                    return located[uid]
                if lookups >= MAX_LOOKUPS or deadline - self._now() < 1.0:
                    return None  # not checked
                lookups += 1
                loc = (await call("get_media", {"uid": uid})).get("location") or {}
                if loc.get("lat") is None or loc.get("lng") is None:
                    located[uid] = False  # no position (e.g. location stripped)
                else:
                    pt = (float(loc["lng"]), float(loc["lat"]))
                    located[uid] = place.shape.distance_m(pt) <= place.tolerance_m + PHOTO_TOLERANCE_M
                return located[uid]

            days, unchecked = [], 0
            for d in sorted(by_day):
                day_items = sorted(by_day[d], key=lambda i: i.get("taken_at_local") or "")
                first = last = None
                for i in day_items[:LOOKUPS_PER_DAY_END]:
                    if (r := await inside(i)) is None:
                        break
                    if r:
                        first = i
                        break
                if first is not None:
                    for i in reversed(day_items[-LOOKUPS_PER_DAY_END:]):
                        if i is first or await inside(i):
                            last = i
                            break
                    last = last or first
                    days.append({
                        "date": d, "evidence": "photo",
                        "first_time": _local_time(first), "last_time": _local_time(last),
                        "photos_nearby": len(day_items),
                        "place": _place_name(first),
                        **({"people": p} if (p := _people(day_items)) else {}),
                        "examples": [t for t in (first.get("title"), last.get("title")) if t][:2],
                    })
                elif any(located.get(i.get("uid")) is None for i in day_items[:LOOKUPS_PER_DAY_END]):
                    unchecked += 1
        out["days"] = days
        if more:
            out["truncated"] = True
        if unchecked:
            out["unchecked_days"] = unchecked
        return out

    async def on_day(self, d: date, deadline: float) -> dict[str, Any]:
        async with self.connect() as call:
            items, more = await self._scan(call, _date_args(d, d), deadline)
        items = [i for i in items if _local_day(i) == d.isoformat()]
        out = self._summary(items, one_day=True)
        if more:
            out["count_is_lower_bound"] = True
        return out

    async def in_range(self, s: date, e: date, deadline: float) -> dict[str, Any]:
        async with self.connect() as call:
            items, more = await self._scan(call, _date_args(s, e), deadline)
        items = [i for i in items if _in_range(_local_day(i), s, e)]
        out = self._summary(items, one_day=s == e)
        per_day = Counter(_local_day(i) for i in items)
        if per_day:
            out["by_day"] = dict(sorted(per_day.items())[:62])
        if more:
            out["count_is_lower_bound"] = True
        return out


def is_person_error(exc: ToolError) -> bool:
    """Unknown/ambiguous person: the model must ask the user, not ignore it."""
    msg = str(exc)
    return msg.startswith(("Unknown person", "Ambiguous person", "Person name is empty"))
