"""Photos module: voice questions about the PhotoPrism library.

"¿Cuándo fuimos a Asturias?", "¿cuántas fotos hay de Carmen en 2019?", "¿qué
álbumes tenemos?", "¿qué hicimos un día como hoy?".

PhotoPrism ships its own read-only MCP server (/api/v1/mcp). This module is a
thin client of it: it resolves spoken names (people, albums) to PhotoPrism
UIDs, pages through results and condenses them into short summaries a voice
answer can use (counts, date range, places, a few examples). It never sees
coordinates (no `places` scope) and cannot change anything: the token's
scope includes `read`, and the PhotoPrism MCP tools are read-only anyway.

Token: a PhotoPrism app password with scope
  "mcp read photos albums people labels"
(photoprism auth add -n familia-mcp -s "<scope>" -e 31536000 <user>).
"""

from __future__ import annotations

import functools
import json
import logging
from collections import Counter
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from datetime import date, datetime
from typing import Annotated, Any, AsyncIterator, Awaitable, Callable, Literal

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .config import PhotosConfig
from .matching import resolve

logger = logging.getLogger("familia_mcp.photos")

PAGE = 50            # PhotoPrism MCP max per call
SCAN_LIMIT = 1000    # max media scanned to answer one question (20 calls)
SAMPLES = 5
TOP_PLACES = 6
MAX_LIST = 25
MAX_YEARS = 15

Kind = Literal["any", "photo", "video"]
_KIND_TYPES = {"any": None, "photo": "image|raw|live", "video": "video"}

# Runs one PhotoPrism MCP tool and returns its JSON result.
CallTool = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
# Opens one MCP session to PhotoPrism and yields a CallTool bound to it.
Connect = Callable[[], AbstractAsyncContextManager[CallTool]]

# The MCP client logs every session at INFO; keep the proxy journal readable.
logging.getLogger("mcp.client").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# Connection to PhotoPrism's MCP endpoint
# --------------------------------------------------------------------------
@asynccontextmanager
async def _session(target: Any) -> AsyncIterator[CallTool]:
    """One MCP session per question (all its pages share it): no state goes
    stale between voice turns and a PhotoPrism restart never breaks us."""
    try:
        async with Client(target) as client:

            async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
                return _result(await client.call_tool(name, args))

            yield call
    except Exception as exc:
        # The MCP client runs inside a task group, so our own ToolErrors
        # ("unknown person", ...) arrive wrapped in an ExceptionGroup: unwrap
        # them so the model gets the real message, not "not reachable".
        own = _find_tool_error(exc)
        if own is not None:
            raise own from None
        logger.warning("PhotoPrism MCP session failed: %r", exc)  # network, auth, protocol
        raise ToolError(f"PhotoPrism is not reachable ({type(_leaf(exc)).__name__}).") from exc


def _find_tool_error(exc: BaseException) -> ToolError | None:
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            if (found := _find_tool_error(sub)) is not None:
                return found
    return None


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def http_connect(cfg: PhotosConfig) -> Connect:
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    @asynccontextmanager
    async def connect() -> AsyncIterator[CallTool]:
        # Each session owns its HTTP client: concurrent questions never share one.
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {cfg.token}"}, timeout=cfg.timeout_s) as http:
            async with _session(streamable_http_client(cfg.mcp_url, http_client=http)) as call:
                yield call

    return connect


def server_connect(target: Any) -> Connect:
    """Same as http_connect, against an in-process MCP server (tests)."""
    return lambda: _session(target)


def _result(res: Any) -> dict[str, Any]:
    text = res.content[0].text if res.content else ""
    if res.is_error:
        raise ToolError(f"PhotoPrism: {text[:200]}")
    if res.structured_content is not None:
        return res.structured_content
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ToolError("PhotoPrism returned an unexpected response.") from exc
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _taken(item: dict[str, Any]) -> datetime | None:
    raw = item.get("taken_at_local") or item.get("taken_at") or ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _place(item: dict[str, Any]) -> str | None:
    place = item.get("place") or {}
    name = place.get("name") if isinstance(place, dict) else None
    return name or None


def _short_place(name: str) -> str:
    """'Níjar, Andalucía, Spain' -> 'Níjar, Andalucía' (enough to say it aloud)."""
    parts = [p.strip() for p in name.split(",") if p.strip()]
    return ", ".join(parts[:2]) if parts else name


def _check_date(value: str | None, field: str) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ToolError(f"{field} must be a date like 2019-07-31.") from exc


# --------------------------------------------------------------------------
# Domain logic
# --------------------------------------------------------------------------
# The session of the question being answered (a ContextVar keeps concurrent
# questions apart). Helpers call self.call(); public methods open the session.
_current_call: ContextVar[CallTool] = ContextVar("photoprism_call")


def _one_session(fn):
    @functools.wraps(fn)
    async def wrapper(self: "Photos", *args, **kwargs):
        async with self.connect() as call:
            token = _current_call.set(call)
            try:
                return await fn(self, *args, **kwargs)
            finally:
                _current_call.reset(token)

    return wrapper


class Photos:
    def __init__(self, connect: Connect):
        self.connect = connect

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        return await _current_call.get()(name, args)

    async def _people(self, query: str | None = None) -> list[dict[str, Any]]:
        out, offset = [], 0
        while True:
            args: dict[str, Any] = {"limit": PAGE, "offset": offset}
            if query:
                args["query"] = query
            page = await self.call("list_people", args)
            items = page.get("items") or []
            out.extend(i for i in items if isinstance(i, dict) and i.get("uid"))
            if not page.get("truncated") or not items:
                return out
            offset += len(items)

    async def _albums(self, query: str | None = None) -> list[dict[str, Any]]:
        out, offset = [], 0
        while True:
            args: dict[str, Any] = {"type": "album", "limit": PAGE, "offset": offset}
            if query:
                args["query"] = query
            page = await self.call("list_collections", args)
            items = page.get("items") or []
            out.extend(i for i in items if isinstance(i, dict) and i.get("uid"))
            if not page.get("truncated") or not items:
                return out
            offset += len(items)

    async def _resolve_person(self, name: str) -> dict[str, Any]:
        people = await self._people()
        return resolve(name, people, names=lambda p: [p.get("name") or ""], label=lambda p: p.get("name") or "?",
                       kind="person")

    async def _resolve_album(self, name: str) -> dict[str, Any]:
        albums = await self._albums()
        return resolve(name, albums, names=lambda a: [a.get("title") or ""], label=lambda a: a.get("title") or "?",
                       kind="album")

    async def _scan(self, args: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
        """All media for a search, up to SCAN_LIMIT. -> (items, more_exist)."""
        items: list[dict[str, Any]] = []
        offset = 0
        while len(items) < SCAN_LIMIT:
            page = await self.call("search_media", {**args, "limit": PAGE, "offset": offset})
            batch = [i for i in page.get("items") or [] if isinstance(i, dict)]
            items.extend(batch)
            nxt = page.get("next_offset")
            if not batch or nxt is None:
                return items, False
            offset = int(nxt)
        return items[:SCAN_LIMIT], True

    @staticmethod
    def _summary(items: list[dict[str, Any]], more: bool) -> dict[str, Any]:
        # Sort by date only: equal timestamps must not compare the dicts.
        dated = sorted(((t, i) for i in items if (t := _taken(i)) is not None), key=lambda ti: ti[0])
        places = Counter(_short_place(p) for i in items if (p := _place(i)))
        kinds = Counter("video" if i.get("type") == "video" else "photo" for i in items)
        out: dict[str, Any] = {
            "count": len(items),
            "count_is_lower_bound": more,  # True: there are more than `count`
            "photos": kinds.get("photo", 0),
            "videos": kinds.get("video", 0),
        }
        if dated:
            out["first_taken"] = dated[0][0].date().isoformat()
            out["last_taken"] = dated[-1][0].date().isoformat()
            years = Counter(t.year for t, _ in dated)
            if len(years) > 1:
                out["by_year"] = dict(sorted(years.items()))
        if places:
            out["top_places"] = [{"place": p, "count": n} for p, n in places.most_common(TOP_PLACES)]
        # Examples spread over the range (first, last and in between).
        if dated:
            step = max(1, len(dated) // SAMPLES)
            picks = dated[::step][: SAMPLES - 1] + [dated[-1]]
            seen, examples = set(), []
            for t, i in picks:
                if i.get("uid") in seen:
                    continue
                seen.add(i.get("uid"))
                examples.append({k: v for k, v in {"date": t.date().isoformat(), "title": i.get("title"),
                                                   "place": _place(i), "favorite": i.get("favorite") or None}.items()
                                 if v})
            out["examples"] = examples
        return out

    @_one_session
    async def search(
        self, text: str | None, person: str | None, album: str | None, year: int | None,
        taken_after: str | None, taken_before: str | None, kind: str,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"order": "newest"}
        criteria: dict[str, Any] = {}
        if text and text.strip():
            args["query"] = text.strip()
            criteria["text"] = args["query"]
        if person:
            p = await self._resolve_person(person)
            args["person"] = p["uid"]
            criteria["person"] = p.get("name")
        if album:
            a = await self._resolve_album(album)
            args["collection"] = a["uid"]
            criteria["album"] = a.get("title")
        after, before = _check_date(taken_after, "taken_after"), _check_date(taken_before, "taken_before")
        if year:
            if not 1900 <= year <= 2100:
                raise ToolError("year must be a 4-digit year.")
            after, before = after or f"{year}-01-01", before or f"{year + 1}-01-01"
            criteria["year"] = year
        if after:
            args["taken_after"] = after
            criteria.setdefault("taken_after", after)
        if before:
            args["taken_before"] = before
            criteria.setdefault("taken_before", before)
        if _KIND_TYPES[kind]:
            args["type"] = _KIND_TYPES[kind]
        if not criteria:
            raise ToolError("Give at least one criterion: text, person, album, year or dates.")
        items, more = await self._scan(args)
        return {"criteria": criteria, **self._summary(items, more)}

    @_one_session
    async def albums(self, query: str | None) -> dict[str, Any]:
        albums = await self._albums(query.strip() if query else None)
        rows = [{"title": a.get("title"), "count": a.get("media_count"),
                 "created": (a.get("created_at") or "")[:10] or None} for a in albums]
        rows.sort(key=lambda r: r["created"] or "", reverse=True)
        return {"total": len(rows), "albums": rows[:MAX_LIST]}

    @_one_session
    async def people(self, query: str | None) -> dict[str, Any]:
        people = await self._people(query.strip() if query else None)
        rows = sorted(({"name": p.get("name"), "count": p.get("media_count")} for p in people),
                      key=lambda r: -(r["count"] or 0))
        return {"total": len(rows), "people": rows[:MAX_LIST]}

    @_one_session
    async def on_this_day(self, month: int | None, day: int | None, today: date | None = None) -> dict[str, Any]:
        today = today or date.today()
        month, day = month or today.month, day or today.day
        if not (1 <= month <= 12 and 1 <= day <= 31):
            raise ToolError("Invalid month/day.")
        items, more = await self._scan({"query": f"month:{month} day:{day}", "order": "oldest"})
        by_year: dict[int, list[dict[str, Any]]] = {}
        for i in items:
            t = _taken(i)
            if t and t.month == month and t.day == day:  # local date, not UTC
                by_year.setdefault(t.year, []).append(i)
        years = []
        for y in sorted(by_year, reverse=True):
            places = Counter(_short_place(p) for i in by_year[y] if (p := _place(i)))
            years.append({"year": y, "count": len(by_year[y]),
                          "places": [p for p, _ in places.most_common(3)]})
        return {"date": f"{day:02d}/{month:02d}", "years_with_photos": len(years),
                "years": years[:MAX_YEARS], "count_is_lower_bound": more}


# --------------------------------------------------------------------------
# MCP tool registration
# --------------------------------------------------------------------------
_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def register(server: MCPServer, photos: Photos) -> None:
    @server.tool(
        name="photos_search",
        description=(
            "Search the family photo library (PhotoPrism) and get a summary: how many photos/videos, "
            "first and last date, counts per year, top places and a few examples. Combine criteria: "
            "person (a recognised face), album, year or dates, free text (place, thing or event, e.g. "
            "'Asturias', 'playa', 'cumpleaños'). Use for 'when did we go to X?' (first/last date) and "
            "'how many photos of Carmen in 2019?'. It cannot show pictures."
        ),
        annotations=_READ_ONLY,
    )
    async def photos_search(
        text: Annotated[str | None, Field(description="Free text: place, object or event.", max_length=100)] = None,
        person: Annotated[str | None, Field(description="Person name as tagged in PhotoPrism (fuzzy-matched).", max_length=60)] = None,
        album: Annotated[str | None, Field(description="Album name (fuzzy-matched).", max_length=100)] = None,
        year: Annotated[int | None, Field(description="Year the photos were taken.", ge=1900, le=2100)] = None,
        taken_after: Annotated[str | None, Field(description="YYYY-MM-DD, inclusive.")] = None,
        taken_before: Annotated[str | None, Field(description="YYYY-MM-DD, exclusive.")] = None,
        kind: Annotated[Kind, Field(description="Only photos or only videos.")] = "any",
    ) -> dict[str, Any]:
        return await photos.search(text, person, album, year, taken_after, taken_before, kind)

    @server.tool(
        name="photos_albums",
        description="Photo albums (newest first) with how many pictures each has. Optional text filter on the title.",
        annotations=_READ_ONLY,
    )
    async def photos_albums(
        query: Annotated[str | None, Field(description="Text to look for in album titles.", max_length=100)] = None,
    ) -> dict[str, Any]:
        return await photos.albums(query)

    @server.tool(
        name="photos_people",
        description="People recognised in the photo library, with how many pictures each appears in.",
        annotations=_READ_ONLY,
    )
    async def photos_people(
        query: Annotated[str | None, Field(description="Text to look for in names.", max_length=60)] = None,
    ) -> dict[str, Any]:
        return await photos.people(query)

    @server.tool(
        name="photos_on_this_day",
        description="'On this day': photos taken on this calendar day (default today) in previous years, per year with places.",
        annotations=_READ_ONLY,
    )
    async def photos_on_this_day(
        month: Annotated[int | None, Field(description="Month 1-12; default today.", ge=1, le=12)] = None,
        day: Annotated[int | None, Field(description="Day 1-31; default today.", ge=1, le=31)] = None,
    ) -> dict[str, Any]:
        return await photos.on_this_day(month, day)
