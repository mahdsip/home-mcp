"""Plex module: read-only questions about the home Plex Media Server.

What the household asks by voice: "¿tenemos Toy Story?", "¿qué hay nuevo?",
"¿por dónde va la serie de los niños?", "¿qué se vio ayer?", "¿quién está
viendo algo ahora?".

Read-only by construction: the client only issues GET requests and only to
the fixed paths in `_ALLOWED_PATHS`. The owner token can do anything on the
server (Plex has no scoped tokens), so this check is the boundary.

Accounts: the owner token (PLEX_TOKEN) sees the whole server; "continue
watching" (On Deck) is per user, so each Plex Home user needs its own server
token (PLEX_USER_TOKENS="Kids=<token>;..."). History and "now playing" come
from the owner token and cover every account.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .config import PlexConfig
from .matching import resolve

logger = logging.getLogger("familia_mcp.plex")

# The only requests this module can make (GET only).
_ALLOWED_PATHS = (
    re.compile(r"^/hubs/search$"),
    re.compile(r"^/library/recentlyAdded$"),
    re.compile(r"^/library/onDeck$"),
    re.compile(r"^/status/sessions$"),
    re.compile(r"^/status/sessions/history/all$"),
    re.compile(r"^/accounts$"),
    re.compile(r"^/devices$"),
)

MAX_RESULTS = 10
MAX_HISTORY = 25
SUMMARY_CHARS = 280
HISTORY_PERIODS = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
OWNER_ACCOUNT_ID = 1  # Plex: the server owner is always account 1 on its own server

Kind = Literal["any", "movie", "show"]
HistoryPeriod = Literal["24h", "7d", "30d"]


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------
class PlexClient:
    def __init__(self, cfg: PlexConfig, transport: httpx.AsyncBaseTransport | None = None):
        self._owner_token = cfg.token
        self._http = httpx.AsyncClient(
            base_url=cfg.url,
            timeout=cfg.timeout_s,
            headers={"Accept": "application/json", "X-Plex-Client-Identifier": "familia-mcp",
                     "X-Plex-Product": "familia-mcp"},
            transport=transport,
        )

    async def get(self, path: str, params: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
        if not any(p.match(path) for p in _ALLOWED_PATHS):
            raise ToolError(f"Plex path not allowed: {path}")  # programming error, never user input
        try:
            resp = await self._http.get(
                path, params=params, headers={"X-Plex-Token": token or self._owner_token}
            )
        except httpx.HTTPError as exc:
            raise ToolError(f"Plex is not reachable ({type(exc).__name__}).") from exc
        if resp.status_code == 401:
            raise ToolError("Plex rejected the access token.")
        if resp.status_code >= 400:
            raise ToolError(f"Plex returned HTTP {resp.status_code}.")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ToolError("Plex returned a non-JSON response.") from exc
        container = data.get("MediaContainer") if isinstance(data, dict) else None
        return container if isinstance(container, dict) else {}


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
def _local(ts: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().isoformat(timespec="minutes")
    except (TypeError, ValueError, OSError):
        return None


def _minutes(ms: Any) -> int | None:
    try:
        return round(int(ms) / 60000)
    except (TypeError, ValueError):
        return None


def _short(text: Any) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    text = " ".join(text.split())
    return text if len(text) <= SUMMARY_CHARS else text[: SUMMARY_CHARS - 1].rsplit(" ", 1)[0] + "…"


def _tags(item: dict[str, Any], key: str, limit: int = 3) -> list[str]:
    return [t["tag"] for t in item.get(key) or [] if isinstance(t, dict) and t.get("tag")][:limit]


def _episode_label(item: dict[str, Any]) -> str | None:
    s, e = item.get("parentIndex"), item.get("index")
    return f"S{int(s):02d}E{int(e):02d}" if isinstance(s, int) and isinstance(e, int) else None


def _title(item: dict[str, Any]) -> dict[str, Any]:
    """Common identification for movies, shows and episodes."""
    kind = item.get("type")
    out: dict[str, Any] = {"type": kind}
    if kind == "episode":
        out["show"] = item.get("grandparentTitle")
        out["episode"] = _episode_label(item)
        out["episode_title"] = item.get("title")
    else:
        out["title"] = item.get("title")
        if item.get("year"):
            out["year"] = item["year"]
    return out


def _progress(item: dict[str, Any]) -> int | None:
    try:
        return round(int(item["viewOffset"]) * 100 / int(item["duration"]))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


# --------------------------------------------------------------------------
# Domain logic
# --------------------------------------------------------------------------
class Plex:
    def __init__(self, client: PlexClient, user_tokens: dict[str, str] | None = None):
        self.client = client
        self.user_tokens = dict(user_tokens or {})

    async def _accounts(self) -> dict[int, str]:
        data = await self.client.get("/accounts")
        return {
            int(a["id"]): a.get("name") or f"account {a['id']}"
            for a in data.get("Account") or []
            if isinstance(a, dict) and str(a.get("id", "")).isdigit() and int(a["id"]) > 0
        }

    async def _resolve_account(self, name: str | None) -> tuple[int, str, str | None]:
        """-> (account id, name, token for per-user endpoints)."""
        accounts = await self._accounts()
        owner = accounts.get(OWNER_ACCOUNT_ID, "owner")
        if not name:
            return OWNER_ACCOUNT_ID, owner, None
        choices = [(i, n) for i, n in accounts.items()]
        acc_id, acc_name = resolve(name, choices, names=lambda c: [c[1]], label=lambda c: c[1], kind="Plex account")
        if acc_id == OWNER_ACCOUNT_ID:
            return acc_id, acc_name, None
        token = next((t for n, t in self.user_tokens.items() if n.casefold() == acc_name.casefold()), None)
        return acc_id, acc_name, token

    async def search(self, query: str, kind: str, limit: int) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            raise ToolError("Search text is empty.")
        data = await self.client.get("/hubs/search", {"query": query, "limit": min(limit, MAX_RESULTS)})
        wanted = {"any": {"movie", "show"}, "movie": {"movie"}, "show": {"show"}}[kind]
        items: list[dict[str, Any]] = []
        for hub in data.get("Hub") or []:
            if hub.get("type") not in wanted:
                continue
            for m in hub.get("Metadata") or []:
                out = _title(m)
                out.update({
                    "summary": _short(m.get("summary")),
                    "genres": _tags(m, "Genre"),
                    "directors": _tags(m, "Director", 2),
                    "actors": _tags(m, "Role", 4),
                    "rating": m.get("audienceRating") or m.get("rating"),
                    "content_rating": m.get("contentRating"),
                    "library": m.get("librarySectionTitle"),
                })
                if m.get("type") == "movie":
                    out["duration_min"] = _minutes(m.get("duration"))
                    out["watched_by_owner"] = bool(m.get("viewCount"))
                else:
                    out["seasons"] = m.get("childCount")
                    out["episodes"] = m.get("leafCount")
                    out["episodes_watched_by_owner"] = m.get("viewedLeafCount")
                items.append({k: v for k, v in out.items() if v not in (None, [], "")})
        return {"query": query, "count": len(items), "results": items[: min(limit, MAX_RESULTS)]}

    async def recently_added(self, kind: str, limit: int) -> dict[str, Any]:
        types = {"any": {"movie", "show", "season", "episode"}, "movie": {"movie"},
                 "show": {"show", "season", "episode"}}[kind]
        data = await self.client.get(
            "/library/recentlyAdded", {"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 50}
        )
        items = []
        for m in data.get("Metadata") or []:
            if m.get("type") not in types:
                continue
            out = _title(m)
            if m.get("type") == "season":  # Plex groups new episodes by season
                out = {"type": "season", "show": m.get("parentTitle"), "season": m.get("title"),
                       "new_episodes": m.get("leafCount")}
            out["added"] = _local(m.get("addedAt"))
            out["library"] = m.get("librarySectionTitle")
            items.append({k: v for k, v in out.items() if v is not None})
            if len(items) >= min(limit, MAX_RESULTS):
                break
        return {"count": len(items), "items": items}

    async def continue_watching(self, account: str | None) -> dict[str, Any]:
        acc_id, name, token = await self._resolve_account(account)
        if acc_id != OWNER_ACCOUNT_ID and token is None:
            # Never fall back to the owner's On Deck: it would answer for the wrong person.
            raise ToolError(f"No access token configured for Plex account '{name}'.")
        data = await self.client.get("/library/onDeck", token=token)
        items, seen = [], set()
        for m in data.get("Metadata") or []:
            key = (m.get("grandparentTitle") or m.get("title"), _episode_label(m))
            if key in seen:  # Plex can list the same item once per library
                continue
            seen.add(key)
            out = _title(m)
            out["progress_pct"] = _progress(m)
            out["last_viewed"] = _local(m.get("lastViewedAt"))
            items.append({k: v for k, v in out.items() if v is not None})
        return {"account": name, "count": len(items), "items": items[:MAX_RESULTS]}

    async def watch_history(self, account: str | None, period: str) -> dict[str, Any]:
        if period not in HISTORY_PERIODS:
            raise ToolError(f"Unknown period '{period}'.")
        accounts = await self._accounts()
        acc_id = None
        if account:
            acc_id, _, _ = await self._resolve_account(account)
        since = datetime.now(timezone.utc) - HISTORY_PERIODS[period]
        params: dict[str, Any] = {
            "sort": "viewedAt:desc",
            "viewedAt>": int(since.timestamp()),
            "X-Plex-Container-Start": 0,
            "X-Plex-Container-Size": 200,
        }
        if acc_id is not None:
            params["accountID"] = acc_id
        data = await self.client.get("/status/sessions/history/all", params)
        devices = await self._devices()
        rows = []
        for h in data.get("Metadata") or []:
            try:
                viewed = int(h.get("viewedAt"))
            except (TypeError, ValueError):
                continue
            if viewed < since.timestamp() or (acc_id is not None and h.get("accountID") != acc_id):
                continue  # server-side filters are best effort; enforce them here
            out = _title(h)
            out["account"] = accounts.get(h.get("accountID"), f"account {h.get('accountID')}")
            out["at"] = _local(viewed)
            out["device"] = devices.get(h.get("deviceID"))
            rows.append({k: v for k, v in out.items() if v is not None})
        by_account: dict[str, int] = {}
        for r in rows:
            by_account[r["account"]] = by_account.get(r["account"], 0) + 1
        return {
            "period": period,
            "account": accounts.get(acc_id) if acc_id is not None else "all",
            "total_views": len(rows),
            "views_by_account": by_account,
            "recent": rows[:MAX_HISTORY],
        }

    async def _devices(self) -> dict[Any, str]:
        try:
            data = await self.client.get("/devices")
        except ToolError:
            return {}  # device names are a nicety; history still works
        return {d.get("id"): d.get("name") for d in data.get("Device") or [] if isinstance(d, dict) and d.get("name")}

    async def now_playing(self) -> dict[str, Any]:
        data = await self.client.get("/status/sessions")
        sessions = []
        for m in data.get("Metadata") or []:
            out = _title(m)
            out["account"] = (m.get("User") or {}).get("title")
            player = m.get("Player") or {}
            out["device"] = player.get("title") or player.get("product")
            out["state"] = player.get("state")
            out["progress_pct"] = _progress(m)
            sessions.append({k: v for k, v in out.items() if v is not None})
        return {"count": len(sessions), "sessions": sessions}


# --------------------------------------------------------------------------
# MCP tool registration
# --------------------------------------------------------------------------
_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def register(server: MCPServer, plex: Plex) -> None:
    # Account names go in the tool descriptions (plain strings): annotations
    # are evaluated against module globals, so they cannot use local values.
    users = ", ".join(sorted(plex.user_tokens)) or "none"
    accounts_note = f" Accounts: omit for the owner (the adults); others: {users} ('Kids' = the children)."

    @server.tool(
        name="plex_search",
        description=(
            "Search the home Plex library for movies and TV shows by title, actor, director or genre. "
            "Returns year, synopsis, duration or seasons, rating and whether the owner watched it. "
            "Library metadata is in English: if a Spanish title finds nothing, retry with the "
            "original/English title (e.g. 'El rey león' -> 'The Lion King')."
        ),
        annotations=_READ_ONLY,
    )
    async def plex_search(
        query: Annotated[str, Field(description="Title, person or genre to search for.", min_length=1, max_length=100)],
        kind: Annotated[Kind, Field(description="Restrict to movies or shows.")] = "any",
        limit: Annotated[int, Field(description="Max results.", ge=1, le=MAX_RESULTS)] = 5,
    ) -> dict[str, Any]:
        return await plex.search(query, kind, limit)

    @server.tool(
        name="plex_recently_added",
        description="Movies and episodes most recently added to Plex, newest first, with the date added.",
        annotations=_READ_ONLY,
    )
    async def plex_recently_added(
        kind: Annotated[Kind, Field(description="Restrict to movies or shows.")] = "any",
        limit: Annotated[int, Field(description="Max items.", ge=1, le=MAX_RESULTS)] = 8,
    ) -> dict[str, Any]:
        return await plex.recently_added(kind, limit)

    @server.tool(
        name="plex_continue_watching",
        description=(
            "What an account has in progress or should watch next (Plex 'On Deck'): next episode of "
            "a series or a half-watched movie, with progress. Use for 'where are the kids in the series?'."
            + accounts_note
        ),
        annotations=_READ_ONLY,
    )
    async def plex_continue_watching(
        account: Annotated[str | None, Field(description="Plex account name; omit for the owner.", max_length=50)] = None,
    ) -> dict[str, Any]:
        return await plex.continue_watching(account)

    @server.tool(
        name="plex_watch_history",
        description=(
            "What was watched on Plex in a recent period: per view the title or episode, account, "
            "local time and device; plus totals per account. Use for 'what did the kids watch today?'."
            + accounts_note
        ),
        annotations=_READ_ONLY,
    )
    async def plex_watch_history(
        account: Annotated[str | None, Field(description="Only this Plex account; omit for everyone.", max_length=50)] = None,
        period: Annotated[HistoryPeriod, Field(description="Look-back window ending now.")] = "24h",
    ) -> dict[str, Any]:
        return await plex.watch_history(account, period)

    @server.tool(
        name="plex_now_playing",
        description="Who is watching what on Plex right now, on which device, playing or paused, and progress.",
        annotations=_READ_ONLY,
    )
    async def plex_now_playing() -> dict[str, Any]:
        return await plex.now_playing()
