"""Location history: "was I at X on day Y?", trips, runs and chargers.

Source: a Nextcloud folder (PHONETRACK_ROOT, e.g. "Maps") read over WebDAV,
GET/PROPFIND only:

    Maps/daily/          PhoneTrack daily GPX exports (timed points, UTC)
    Maps/<trip folder>/  one folder per trip; dates in the folder name
    Maps/SportTracker/   Sports Tracker sessions (runs)
    Maps/Cargadores/     EV chargers (GPX waypoints)

Everything is copied into a local SQLite index (PHONETRACK_DB, chmod 600: it
holds the household's location history). Sync is incremental: Nextcloud
changes a folder's ETag whenever something inside changes, so an unchanged
tree costs a single PROPFIND. `familia-mcp --sync-locations` builds the index
the first time; tool calls then refresh it at most every
PHONETRACK_SYNC_INTERVAL_S, spending at most PHONETRACK_SYNC_BUDGET_S.

Local day and time of a point come from its own coordinates (tzfpy), so a day
in California is California's day. Place names are resolved with Nominatim
(OpenStreetMap): place -> geometry for the "was I there" questions, and
coordinates -> name for day/trip summaries (the household accepted sending
coordinates to the public instance). Results are cached forever.

Every answer says what it is based on (`evidence`):
    gps            a timed fix within the tolerance of the place
    gps_path       the straight line between two consecutive fixes crosses it
    track          an untimed recorded track (dated by file/folder name)
    planned_route  a planned route (<rtept>), not proof of having been there
    marked_place   a place marked in a trip's list, not proof either
"""
from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import math
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Iterable
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import tzfpy
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import __version__, tracks
from .locphotos import PhotoEvidence, is_person_error
from .config import LocationsConfig
from .geo import BBox, Shape, haversine_m
from .matching import normalize, resolve, words
from .nextcloud import ssl_context

logger = logging.getLogger("familia_mcp.phonetrack")

SUPPORTED = (".gpx", ".kml")
# Bump when parsing or dating rules change: the next sync re-reads every file.
PARSER_VERSION = "2"
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_GAP_S = 2 * 3600            # fixes further apart are not joined into a path
MAX_ACCURACY_M = 1000.0         # fixes worse than this are ignored
LINE_TOLERANCE_M = 50.0         # streets / POIs: distance that counts as "passed by"
STOP_RADIUS_M = 250.0
STOP_MIN_S = 30 * 60
MAX_REVERSE_PER_CALL = 10       # Nominatim allows 1/s: keeps a cold summary around 10 s
DAY_SAMPLE_M = 5000             # where_was_i: one locality lookup per this distance travelled
TRIP_SAMPLE_M = 15000           # trip_summary: idem
MAX_RESULTS = 30
# Radius for places Nominatim only knows as a point (no boundary polygon).
POINT_RADIUS_M = {"city": 3000, "town": 1500, "village": 700, "hamlet": 300, "suburb": 600,
                  "quarter": 400, "neighbourhood": 300, "island": 2000, "locality": 300}
_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS folders(path TEXT PRIMARY KEY, etag TEXT, category TEXT, name TEXT,
    date_from TEXT, date_to TEXT, explicit_range INTEGER);
CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, folder TEXT, etag TEXT,
    category TEXT NOT NULL, name TEXT, date_from TEXT, date_to TEXT, date_source TEXT,
    n_points INTEGER, n_timed INTEGER, n_waypoints INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS points(file_id INTEGER NOT NULL, seg INTEGER, seq INTEGER, lon REAL, lat REAL,
    ts INTEGER, local_date TEXT, local_time TEXT, tz TEXT, accuracy REAL, kind TEXT);
CREATE INDEX IF NOT EXISTS points_date ON points(local_date);
CREATE INDEX IF NOT EXISTS points_file ON points(file_id, seg, seq);
CREATE TABLE IF NOT EXISTS waypoints(file_id INTEGER NOT NULL, name TEXT, descr TEXT, type TEXT,
    lon REAL, lat REAL, ts INTEGER, local_date TEXT);
CREATE INDEX IF NOT EXISTS waypoints_file ON waypoints(file_id);
CREATE TABLE IF NOT EXISTS geocode(key TEXT PRIMARY KEY, result TEXT, fetched_at INTEGER);
"""


# ---------------------------------------------------------------- time zones

_TZ_CACHE: dict[str, ZoneInfo] = {}


def zone_at(lon: float, lat: float) -> ZoneInfo:
    name = tzfpy.get_tz(lon, lat) or "UTC"
    if name not in _TZ_CACHE:
        try:
            _TZ_CACHE[name] = ZoneInfo(name)
        except ZoneInfoNotFoundError:
            _TZ_CACHE[name] = ZoneInfo("UTC")
    return _TZ_CACHE[name]


def parse_day(raw: str | None, what: str) -> date | None:
    if raw is None or not raw.strip():
        return None
    raw = raw.strip()
    if not _ISO_DATE.match(raw):
        raise ToolError(f"{what} must be a date like 2026-11-08.")
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ToolError(f"{what} is not a valid date.") from exc


def _hhmm(ts: int, tz: str) -> str:
    return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%H:%M")


# ---------------------------------------------------------------- WebDAV files (read-only)

@dataclass
class DavEntry:
    path: str          # relative to the user's files home, e.g. "Maps/daily/x.gpx"
    is_dir: bool
    etag: str
    size: int


class FilesClient:
    """PROPFIND / GET under one folder of the user's Nextcloud files."""

    def __init__(self, cfg: LocationsConfig, transport: httpx.AsyncBaseTransport | None = None):
        nc = cfg.nextcloud
        self.home = f"/remote.php/dav/files/{quote(nc.user, safe='')}/"
        self.root = cfg.root.strip("/") + "/"
        self._netloc = urlsplit(nc.url).netloc
        self._http = httpx.AsyncClient(
            base_url=nc.url, auth=(nc.user, nc.app_password), timeout=cfg.timeout_s,
            verify=ssl_context(nc.cert_file), headers={"User-Agent": "home-mcp"},
            transport=transport, follow_redirects=False,
        )

    def _url(self, rel: str) -> str:
        parts = rel.split("/")
        if ".." in parts or not (rel + "/").startswith(self.root):
            raise ToolError("Nextcloud path not allowed.")
        return self.home + quote(rel, safe="/")

    async def _request(self, method: str, rel: str, **kw: Any) -> httpx.Response:
        try:
            resp = await self._http.request(method, self._url(rel), **kw)
        except httpx.HTTPError as exc:
            raise ToolError(f"Nextcloud is not reachable ({type(exc).__name__}).") from exc
        if resp.status_code == 401:
            raise ToolError("Nextcloud rejected the user or app password.")
        if resp.status_code == 404:
            raise ToolError(f"Nextcloud folder or file not found: {rel}")
        if resp.status_code >= 400:
            raise ToolError(f"Nextcloud returned HTTP {resp.status_code}.")
        return resp

    async def list(self, rel_dir: str) -> list[DavEntry]:
        body = ('<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
                "<d:getetag/><d:resourcetype/><d:getcontentlength/></d:prop></d:propfind>")
        resp = await self._request("PROPFIND", rel_dir.rstrip("/") + "/", content=body.encode(),
                                   headers={"Depth": "1", "Content-Type": "application/xml"})
        out = []
        for r in ET.fromstring(resp.content).findall("{DAV:}response"):
            href = r.findtext("{DAV:}href") or ""
            parts = urlsplit(href)
            if parts.netloc and parts.netloc != self._netloc:
                continue
            path = unquote(parts.path)
            if not path.startswith(self.home):
                continue
            rel = path[len(self.home):]
            is_dir = r.find(".//{DAV:}resourcetype/{DAV:}collection") is not None
            rel = rel.rstrip("/")
            if rel == rel_dir.rstrip("/"):
                out.insert(0, DavEntry(rel, is_dir, (r.findtext(".//{DAV:}getetag") or "").strip('"'), 0))
                continue
            size = r.findtext(".//{DAV:}getcontentlength")
            out.append(DavEntry(rel, is_dir, (r.findtext(".//{DAV:}getetag") or "").strip('"'),
                                int(size) if size and size.isdigit() else 0))
        return out

    async def get(self, rel: str) -> bytes:
        resp = await self._request("GET", rel)
        if len(resp.content) > MAX_FILE_BYTES:
            raise ToolError(f"File too large to index: {rel}")
        return resp.content

    async def aclose(self) -> None:
        await self._http.aclose()


# ---------------------------------------------------------------- index

class Index:
    def __init__(self, path: str):
        self.path = path
        # Location history: owner only. The directory must be private because
        # SQLite creates the -wal/-shm files (and we the sync lock) next to the
        # database with the process umask.
        p = Path(path)
        if not p.parent.exists():
            p.parent.mkdir(parents=True, mode=0o700)
            os.chmod(p.parent, 0o700)  # mkdir's mode is filtered by the umask
        elif p.parent.stat().st_mode & 0o077:
            raise ValueError(f"PHONETRACK_DB must be in a directory only the service user can open "
                             f"(chmod 700 {p.parent})")
        self.db = sqlite3.connect(path, timeout=30)
        os.chmod(path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    def invalidate(self) -> None:
        """Forget every ETag so the next sync re-reads all files."""
        with self.db:
            self.db.execute("UPDATE files SET etag = NULL")
            self.db.execute("UPDATE folders SET etag = NULL")
            self.db.execute("DELETE FROM meta WHERE key = 'root_etag'")

    def is_empty(self) -> bool:
        return self.db.execute("SELECT 1 FROM files LIMIT 1").fetchone() is None

    def cache_get(self, key: str) -> Any:
        row = self.db.execute("SELECT result FROM geocode WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, key: str, value: Any) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO geocode(key, result, fetched_at) VALUES (?, ?, ?)",
                            (key, json.dumps(value), int(time.time())))

    def replace_file(self, *, path: str, folder: str, etag: str, category: str,
                     dates: tracks.DateRange | None, date_source: str,
                     parsed: tracks.Parsed | None, error: str | None) -> None:
        name = path.rsplit("/", 1)[-1]
        with self.db:
            old = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            if old:
                self._delete_ids([old[0]])
            pts = parsed.points if parsed else []
            wps = parsed.waypoints if parsed else []
            cur = self.db.execute(
                "INSERT INTO files(path, folder, etag, category, name, date_from, date_to, date_source,"
                " n_points, n_timed, n_waypoints, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (path, folder, etag, category, name,
                 dates[0].isoformat() if dates else None, dates[1].isoformat() if dates else None,
                 date_source, len(pts), sum(1 for p in pts if p.ts), len(wps), error))
            fid = cur.lastrowid
            rows = []
            for seq, p in enumerate(pts):
                ts = ld = lt = tzname = None
                if p.ts:
                    z = zone_at(p.lon, p.lat)
                    local = p.ts.astimezone(z)
                    ts, ld, lt, tzname = int(p.ts.timestamp()), local.date().isoformat(), local.strftime("%H:%M"), z.key
                rows.append((fid, p.seg, seq, p.lon, p.lat, ts, ld, lt, tzname, p.accuracy, p.kind))
            self.db.executemany("INSERT INTO points VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            wrows = []
            for w in wps:
                ts = ld = None
                if w.ts:
                    ts, ld = int(w.ts.timestamp()), w.ts.astimezone(zone_at(w.lon, w.lat)).date().isoformat()
                wrows.append((fid, w.name, w.desc, w.type, w.lon, w.lat, ts, ld))
            self.db.executemany("INSERT INTO waypoints VALUES (?,?,?,?,?,?,?,?)", wrows)

    def _delete_ids(self, ids: list[int]) -> None:
        for fid in ids:
            self.db.execute("DELETE FROM points WHERE file_id=?", (fid,))
            self.db.execute("DELETE FROM waypoints WHERE file_id=?", (fid,))
            self.db.execute("DELETE FROM files WHERE id=?", (fid,))

    def delete_paths(self, paths: Iterable[str]) -> None:
        with self.db:
            for path in paths:
                row = self.db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
                if row:
                    self._delete_ids([row[0]])

    def file_etags(self, folder: str) -> dict[str, str]:
        return {r["path"]: r["etag"] for r in self.db.execute("SELECT path, etag FROM files WHERE folder=?", (folder,))}

    def folder_etags(self) -> dict[str, str]:
        return {r["path"]: r["etag"] for r in self.db.execute("SELECT path, etag FROM folders")}

    def put_folder(self, path: str, etag: str | None, category: str, name: str,
                   dates: tuple[tracks.DateRange, bool] | None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO folders(path, etag, category, name, date_from, date_to, explicit_range)"
                " VALUES (?,?,?,?,?,?,?)",
                (path, etag, category, name, dates[0][0].isoformat() if dates else None,
                 dates[0][1].isoformat() if dates else None, int(dates[1]) if dates else None))

    def delete_folder(self, path: str) -> None:
        with self.db:
            ids = [r[0] for r in self.db.execute("SELECT id FROM files WHERE folder=?", (path,))]
            self._delete_ids(ids)
            self.db.execute("DELETE FROM folders WHERE path=?", (path,))


# ---------------------------------------------------------------- Nominatim

@dataclass
class Place:
    name: str
    shape: Shape
    kind: str              # area | line | point
    tolerance_m: float     # extra distance that still counts as "there"
    osm_class: str = ""
    osm_type: str = ""


class Nominatim:
    """Search / reverse with Nominatim's usage policy: 1 request per second,
    an identifying User-Agent, results cached."""

    def __init__(self, cfg: LocationsConfig, index: Index, transport: httpx.AsyncBaseTransport | None = None,
                 min_interval_s: float = 1.05):
        headers = {"User-Agent": f"home-mcp/{__version__} (personal assistant; https://github.com/mahdsip/home-mcp)"}
        if cfg.nominatim_language:
            headers["Accept-Language"] = cfg.nominatim_language
        self._http = httpx.AsyncClient(base_url=cfg.nominatim_url, timeout=cfg.timeout_s, headers=headers,
                                       transport=transport)
        self.email = cfg.nominatim_email
        self.index = index
        self.min_interval_s = min_interval_s
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.calls = 0

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if self.email:
            params["email"] = self.email
        async with self._lock:
            wait = self._last + self.min_interval_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                resp = await self._http.get(path, params=params)
            except httpx.HTTPError as exc:
                raise ToolError(f"The map service (Nominatim) is not reachable ({type(exc).__name__}).") from exc
            finally:
                self._last = time.monotonic()
                self.calls += 1
        if resp.status_code in (403, 429):
            raise ToolError("The map service (Nominatim) is rate-limiting us; try again in a minute.")
        if resp.status_code >= 400:
            raise ToolError(f"The map service (Nominatim) returned HTTP {resp.status_code}.")
        return resp.json()

    async def search(self, query: str, *, limit: int = 5, dedupe: bool = True) -> list[dict[str, Any]]:
        key = f"search:{int(dedupe)}:{limit}:{' '.join(query.lower().split())}"
        cached = self.index.cache_get(key)
        if cached is not None:
            return cached
        params: dict[str, Any] = {"q": query, "format": "jsonv2", "limit": limit, "polygon_geojson": 1,
                                  "polygon_threshold": 0.0001, "addressdetails": 0}
        if not dedupe:
            params["dedupe"] = 0
        result = await self._get("/search", params)
        result = result if isinstance(result, list) else []
        self.index.cache_put(key, result)
        return result

    async def reverse(self, lon: float, lat: float, zoom: int) -> dict[str, Any]:
        # City-level answers are shared by everything within ~1 km; street-level within ~100 m.
        key = f"reverse:{zoom}:{lat:.2f}:{lon:.2f}" if zoom <= 10 else f"reverse:{zoom}:{lat:.3f}:{lon:.3f}"
        cached = self.index.cache_get(key)
        if cached is not None:
            return cached
        result = await self._get("/reverse", {"lat": f"{lat:.5f}", "lon": f"{lon:.5f}", "format": "jsonv2",
                                              "zoom": zoom, "addressdetails": 1})
        result = result if isinstance(result, dict) else {}
        self.index.cache_put(key, result)
        return result

    async def aclose(self) -> None:
        await self._http.aclose()


def _to_place(r: dict[str, Any]) -> Place | None:
    geom = r.get("geojson")
    try:
        shape = Shape.from_geojson(geom) if isinstance(geom, dict) else None
    except (ValueError, TypeError, IndexError):
        shape = None
    if shape is None:
        try:
            shape = Shape(points=[(float(r["lon"]), float(r["lat"]))])
        except (KeyError, TypeError, ValueError):
            return None
    cls, typ = r.get("category") or r.get("class") or "", r.get("type") or ""
    name = r.get("display_name") or r.get("name") or ""
    if shape.is_area:
        return Place(name, shape, "area", 0.0, cls, typ)
    if shape.lines:
        return Place(name, shape, "line", LINE_TOLERANCE_M, cls, typ)
    radius = POINT_RADIUS_M.get(r.get("addresstype") or typ, LINE_TOLERANCE_M * 2)
    return Place(name, shape, "point", float(radius), cls, typ)


def locality_of(rev: dict[str, Any]) -> str | None:
    a = rev.get("address") or {}
    for key in ("city", "town", "village", "municipality", "hamlet", "county", "state"):
        if a.get(key):
            parts = [a[key]]
            if a.get("country") and a.get("country_code") != "es":
                parts.append(a["country"])
            return ", ".join(parts)
    return rev.get("name") or None


def street_of(rev: dict[str, Any]) -> str | None:
    a = rev.get("address") or {}
    head = rev.get("name") if rev.get("category") not in ("highway", "place", "boundary") else None
    street = a.get("road") or a.get("pedestrian") or a.get("footway")
    area = a.get("suburb") or a.get("neighbourhood") or a.get("quarter")
    parts = [p for p in (head, street, area) if p]
    loc = locality_of(rev)
    if loc:
        parts.append(loc)
    return ", ".join(dict.fromkeys(parts)) or None


# ---------------------------------------------------------------- the service

@dataclass
class _Pt:
    file_id: int
    seg: int
    lon: float
    lat: float
    ts: int | None
    local_date: str | None
    local_time: str | None
    tz: str | None
    accuracy: float | None
    kind: str


@dataclass
class _Hit:
    distance_m: float
    via: str              # point | path
    a: _Pt
    b: _Pt | None = None


@dataclass
class SyncStats:
    folders_listed: int = 0
    files_indexed: int = 0
    files_removed: int = 0
    errors: list[str] = field(default_factory=list)
    complete: bool = True
    skipped: str | None = None


class Locations:
    def __init__(self, cfg: LocationsConfig, files: FilesClient, index: Index, geocoder: Nominatim,
                 now: Any = None, photos: PhotoEvidence | None = None):
        self.cfg = cfg
        self.files = files
        self.index = index
        self.geo = geocoder
        self.photos = photos  # PhotoPrism evidence; None when the photos module is not configured
        self._now = now or time.time
        self._sync_lock = asyncio.Lock()
        self._background: asyncio.Task | None = None

    # ------------------------------------------------ sync

    def _category(self, folder_name: str | None) -> str:
        if folder_name == self.cfg.daily_folder:
            return "daily"
        if folder_name == self.cfg.sport_folder:
            return "sport"
        if folder_name == self.cfg.chargers_folder:
            return "chargers"
        return "trip"

    async def sync(self, budget_s: float | None = None) -> SyncStats:
        stats = SyncStats()
        lock_path = self.cfg.db_path + ".lock"
        async with self._sync_lock:
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    stats.skipped = "another process is syncing"
                    stats.complete = False
                    return stats
                await self._sync_locked(stats, None if budget_s is None else time.monotonic() + budget_s)
            finally:
                os.close(fd)
        if stats.complete:
            self.index.set_meta("last_sync", str(self._now()))
        return stats

    async def _sync_locked(self, stats: SyncStats, deadline: float | None) -> None:
        if self.index.meta("parser_version") != PARSER_VERSION:
            self.index.invalidate()
            self.index.set_meta("parser_version", PARSER_VERSION)
        root = self.cfg.root.strip("/")
        entries = await self.files.list(root)
        stats.folders_listed += 1
        root_etag = entries[0].etag if entries and entries[0].path == root else ""
        if root_etag and self.index.meta("root_etag") == root_etag:
            return
        known_folders = self.index.folder_etags()
        seen_folders = {root}
        # files directly under the root: a "trip" each, undated unless the name says so
        await self._sync_folder(root, None, [e for e in entries[1:] if not e.is_dir], stats, deadline)
        for e in entries[1:]:
            if not e.is_dir:
                continue
            name = e.path.rsplit("/", 1)[-1]
            if name.startswith("."):
                continue
            seen_folders.add(e.path)
            if known_folders.get(e.path) == e.etag:
                continue
            if deadline and time.monotonic() > deadline:
                stats.complete = False
                break
            try:
                children = await self.files.list(e.path)
                stats.folders_listed += 1
            except ToolError as exc:
                stats.errors.append(f"{e.path}: {exc}")
                stats.complete = False
                continue
            errors_before = len(stats.errors)
            done = await self._sync_folder(e.path, name, [c for c in children[1:] if not c.is_dir], stats, deadline)
            category = self._category(name)
            dates = tracks.folder_dates(name) if category == "trip" else None
            clean = done and len(stats.errors) == errors_before
            # Only a fully read folder keeps its ETag; otherwise the next sync lists it again.
            self.index.put_folder(e.path, e.etag if clean else None, category, name, dates)
            if not done:
                stats.complete = False
                break
        for gone in set(known_folders) - seen_folders:
            self.index.delete_folder(gone)
        if stats.complete and not stats.errors:
            self.index.set_meta("root_etag", root_etag)

    async def _sync_folder(self, folder: str, name: str | None, children: list[DavEntry], stats: SyncStats,
                           deadline: float | None) -> bool:
        category = self._category(name) if name is not None else "trip"
        f_dates = tracks.folder_dates(name) if (name and category == "trip") else None
        known = self.index.file_etags(folder)
        wanted = [c for c in children if c.path.lower().endswith(SUPPORTED)
                  and not c.path.rsplit("/", 1)[-1].startswith(".")]
        for c in wanted:
            if known.get(c.path) == c.etag:
                continue
            if deadline and time.monotonic() > deadline:
                return False
            fname = c.path.rsplit("/", 1)[-1]
            dates, source = tracks.file_dates(fname, f_dates)
            parsed, error = None, None
            try:
                if c.size > MAX_FILE_BYTES:
                    error = f"too large to index ({c.size} bytes)"
                else:
                    data = await self.files.get(c.path)
                    parsed = await asyncio.to_thread(tracks.parse_file, fname, data)
                    if f_dates:
                        _drop_foreign_times(parsed, f_dates[0])
            except ToolError as exc:
                stats.errors.append(f"{c.path}: {exc}")
                continue  # retried next sync (etag not stored)
            except (ET.ParseError, ValueError) as exc:
                error = f"unreadable: {exc}"[:200]
            self.index.replace_file(path=c.path, folder=folder, etag=c.etag, category=category, dates=dates,
                                    date_source=source, parsed=parsed, error=error)
            stats.files_indexed += 1
        gone = set(known) - {c.path for c in wanted}
        if gone:
            self.index.delete_paths(gone)
            stats.files_removed += len(gone)
        return True

    async def ensure_fresh(self) -> dict[str, Any]:
        """Refresh the index if it is older than the sync interval. Never fails
        a question because Nextcloud is down: answers then come from the index."""
        if self.index.is_empty():
            raise ToolError("The location index is empty. Build it once on the MCP Pi with "
                            "`familia-mcp --sync-locations`.")
        if self._background and not self._background.done():
            return {"index_note": "Index refresh in progress; the newest files may be missing."}
        last = float(self.index.meta("last_sync") or 0)
        if self._now() - last < self.cfg.sync_interval_s:
            return {}
        try:
            stats = await self.sync(self.cfg.sync_budget_s)
        except ToolError as exc:
            logger.warning("location sync failed: %s", exc)
            return {"index_note": "Could not refresh from Nextcloud; answer uses the last synced data."}
        if not stats.complete and not stats.skipped:
            # Finish without blocking the answer (the server process outlives the call).
            self._background = asyncio.create_task(self._finish_sync())
            return {"index_note": "Index refresh still in progress; the newest files may be missing."}
        return {}

    async def _finish_sync(self) -> None:
        try:
            await self.sync(None)
        except Exception:  # noqa: BLE001 - background task: log, never raise
            logger.exception("background location sync failed")

    def _deadline(self) -> float:
        return time.monotonic() + self.cfg.tool_budget_s

    @staticmethod
    def _time_left(deadline: float) -> bool:
        """Room for one more Nominatim lookup (1 per second) before the deadline."""
        return deadline - time.monotonic() > 2.0

    # ------------------------------------------------ helpers

    def _points(self, where: str, args: tuple, categories: tuple[str, ...]) -> list[_Pt]:
        q = ("SELECT p.file_id, p.seg, p.lon, p.lat, p.ts, p.local_date, p.local_time, p.tz, p.accuracy, p.kind "
             "FROM points p JOIN files f ON f.id = p.file_id "
             f"WHERE f.category IN ({','.join('?' * len(categories))}) AND ({where}) "
             "ORDER BY p.file_id, p.seg, p.seq")
        return [_Pt(*row) for row in self.index.db.execute(q, (*categories, *args))]

    def _files(self, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
        ids = list(set(ids))
        if not ids:
            return {}
        rows = self.index.db.execute(
            f"SELECT f.*, d.name AS folder_name, d.date_from AS folder_from, d.date_to AS folder_to "
            f"FROM files f LEFT JOIN folders d ON d.path = f.folder WHERE f.id IN ({','.join('?' * len(ids))})", ids)
        return {r["id"]: r for r in rows}

    async def resolve_place(self, place: str) -> list[Place]:
        place = (place or "").strip()
        if not place:
            raise ToolError("Say which place.")
        if len(place) > 200:
            raise ToolError("Place name too long.")
        results = await self.geo.search(place, limit=5)
        places = [p for r in results if (p := _to_place(r))]
        if not places:
            raise ToolError(f"The map service does not know '{place}'. Try adding the town or country.")
        first = places[0]
        if first.kind == "line" and first.osm_class == "highway":
            # A street is many OSM ways: gather the ones with the same name nearby.
            name = (results[0].get("name") or "").strip()
            more = await self.geo.search(place, limit=40, dedupe=False)
            box = first.shape.bbox().expand(5000)
            for r in more:
                p = _to_place(r)
                if (p and p.kind == "line" and (r.get("name") or "").strip() == name
                        and box.contains(p.shape.bbox().center)):
                    first.shape.lines.extend(p.shape.lines)
        return places

    @staticmethod
    def _tolerance(place: Place, pt: _Pt) -> float:
        acc = min(pt.accuracy or 0.0, 200.0)
        return place.tolerance_m + (acc if place.kind != "area" else min(acc, 50.0))

    def _hits(self, place: Place, pts: list[_Pt]) -> list[_Hit]:
        """Points within tolerance of the place, plus path segments that cross it."""
        box = place.shape.bbox().expand(place.tolerance_m + 250)
        hits: list[_Hit] = []
        prev: _Pt | None = None
        for p in pts:
            if p.accuracy is not None and p.accuracy > MAX_ACCURACY_M:
                continue
            if box.contains((p.lon, p.lat)):
                d = place.shape.distance_m((p.lon, p.lat))
                if d <= self._tolerance(place, p):
                    hits.append(_Hit(d, "point", p))
                    prev = p
                    continue
            if prev and prev.file_id == p.file_id and prev.seg == p.seg and (
                    p.ts is None or prev.ts is None or 0 <= p.ts - prev.ts <= MAX_GAP_S):
                a, b = (prev.lon, prev.lat), (p.lon, p.lat)
                if box.intersects_segment(a, b):
                    d = place.shape.segment_distance_m(a, b)
                    if d <= place.tolerance_m:
                        hits.append(_Hit(d, "path", prev, p))
            prev = p
        return hits

    # ------------------------------------------------ tools

    async def was_i_at(self, place: str, day: str | None, start: str | None, end: str | None,
                       person: str | None = None) -> dict[str, Any]:
        deadline = self._deadline()
        person = (person or "").strip() or None
        if person and not self.photos:
            raise ToolError("Asking about another person needs the photo library (PhotoPrism), which is not configured.")
        d = parse_day(day, "date")
        s, e = (d, d) if d else (parse_day(start, "start"), parse_day(end, "end"))
        if s and e and e < s:
            raise ToolError("end is before start.")
        note = await self.ensure_fresh()
        cats = ("daily", "trip")
        timed_where, timed_args = "p.ts IS NOT NULL", ()
        undated_where, undated_args = "p.ts IS NULL", ()
        if s or e:
            lo, hi = (s or date(1900, 1, 1)).isoformat(), (e or date(2999, 1, 1)).isoformat()
            timed_where, timed_args = "p.ts IS NOT NULL AND p.local_date BETWEEN ? AND ?", (lo, hi)
            undated_where = "p.ts IS NULL AND f.date_from <= ? AND f.date_to >= ?"
            undated_args = (hi, lo)
        # The GPS history is the phone owner's: for another person only photos count.
        timed = [] if person else self._points(timed_where, timed_args, cats)
        undated = [] if person else self._points(undated_where, undated_args, cats)
        candidates = await self.resolve_place(place)
        # Ambiguous names ("Gran Vía"): prefer the candidate the user actually went near.
        chosen, hits_t, hits_u = candidates[0], [], []
        for cand in candidates:
            ht = self._hits(cand, timed)
            hu = self._hits(cand, undated)
            if ht or hu:
                chosen, hits_t, hits_u = cand, ht, hu
                break
        out: dict[str, Any] = {}
        visits = self._visits_by_day(hits_t)
        photo_days: list[dict[str, Any]] = []
        if self.photos:
            # GPS near a candidate decided the place; otherwise let the photos
            # pick among the top candidates (photo-only days, other people).
            first: dict[str, Any] | None = None
            for cand in ([chosen] if hits_t or hits_u else candidates[:3]):
                ph = await self._photo_evidence(cand, s, e, person, deadline, out)
                if ph is None:
                    break  # library unreachable; noted in out
                first = first or ph
                if ph.get("days"):
                    chosen, first = cand, ph
                    break
            if first is not None:
                photo_days = self._merge_photo_days(first, {v["date"] for v in visits}, out)
        out.update({"place": chosen.name, "place_kind": chosen.kind, "period": self._period(s, e)})
        out["visits"] = visits[:MAX_RESULTS]
        if len(visits) > MAX_RESULTS:
            out["more_days"] = len(visits) - MAX_RESULTS
        out["found"] = bool(visits or photo_days)
        undated_out = [] if person else self._undated_hits(hits_u)
        wp = [] if person else self._waypoint_hits(chosen, s, e)
        if undated_out or wp:
            out["other_evidence"] = (undated_out + wp)[:MAX_RESULTS]
        if s and e and (e - s).days <= 62 and not person:
            have = {r[0] for r in self.index.db.execute(
                "SELECT DISTINCT p.local_date FROM points p JOIN files f ON f.id=p.file_id "
                "WHERE f.category IN ('daily','trip') AND p.ts IS NOT NULL AND p.local_date BETWEEN ? AND ?",
                (s.isoformat(), e.isoformat()))}
            missing = [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)
                       if (s + timedelta(days=i)).isoformat() not in have]
            if missing:
                out["days_without_gps_data"] = _day_ranges(missing)
        if not out["found"]:
            out["hint"] = ("No photo taken there in the period." if person else
                           "No GPS fix or photo there in the period. 'days_without_gps_data' are days with no GPS "
                           "data at all (the phone did not record), which is not the same as not having been there.")
        if len(candidates) > 1 and chosen is candidates[0] and not out["found"]:
            out["other_places_with_that_name"] = list(dict.fromkeys(
                c.name for c in candidates[1:] if c.name != chosen.name))[:3]
        out.update(note)
        return out

    async def _photo_evidence(self, place: Place, s: date | None, e: date | None, person: str | None,
                              deadline: float, out: dict[str, Any]) -> dict[str, Any] | None:
        """PhotoPrism evidence for one place; None (noted in out) when the library fails,
        which never fails the answer."""
        try:
            return await self.photos.near(place, s, e, person, deadline)  # type: ignore[union-attr]
        except ToolError as exc:
            if person and is_person_error(exc):
                raise
            logger.warning("photo evidence failed: %s", exc)
            out["photos_note"] = "The photo library could not be checked; the answer uses GPS only."
            return None

    @staticmethod
    def _merge_photo_days(ph: dict[str, Any], gps_days: set[str], out: dict[str, Any]) -> list[dict[str, Any]]:
        if ph.get("person"):
            out["person"] = ph["person"]
        days = ph.get("days") or []
        for day in days:
            day["gps_that_day"] = day["date"] in gps_days
        if days:
            out["photo_days"] = days[:MAX_RESULTS]
        if ph.get("truncated") or ph.get("unchecked_days"):
            out["photos_note"] = "Too many photos to check them all; more days may exist."
        return days

    async def _photo_summary(self, out: dict[str, Any], query: Any) -> None:
        """Photos of a day / trip into out["photos"] (only when there are some)."""
        if query is None:
            return
        try:
            ph = await query
        except ToolError as exc:
            logger.warning("photo summary failed: %s", exc)
            out["photos_note"] = "The photo library could not be checked."
            return
        if ph.get("count"):
            out["photos"] = ph

    @staticmethod
    def _period(s: date | None, e: date | None) -> str:
        if not s and not e:
            return "all time"
        if s == e:
            return s.isoformat()  # type: ignore[union-attr]
        return f"{s.isoformat() if s else '…'} to {e.isoformat() if e else '…'}"

    def _visits_by_day(self, hits: list[_Hit]) -> list[dict[str, Any]]:
        days: dict[str, dict[str, Any]] = {}
        files = self._files(h.a.file_id for h in hits)
        for h in hits:
            day = h.a.local_date or ""
            v = days.setdefault(day, {"date": day, "_ts": [], "_dist": math.inf, "_via": set(), "_files": set(),
                                      "tz": h.a.tz})
            v["_ts"].append((h.a.ts, h.a.tz))
            if h.b and h.b.ts:
                v["_ts"].append((h.b.ts, h.b.tz))
            v["_dist"] = min(v["_dist"], h.distance_m)
            v["_via"].add(h.via)
            f = files.get(h.a.file_id)
            if f:
                v["_files"].add(f["folder_name"] if f["category"] == "trip" and f["folder_name"] else "daily GPS")
        out = []
        for day in sorted(days):
            v = days[day]
            ts = sorted(set(v["_ts"]))
            out.append({
                "date": day,
                "first_time": _hhmm(ts[0][0], ts[0][1]),
                "last_time": _hhmm(ts[-1][0], ts[-1][1]),
                "time_zone": v["tz"],
                "evidence": "gps" if "point" in v["_via"] else "gps_path",
                "closest_m": round(v["_dist"]),
                "sources": sorted(v["_files"])[:5],
            })
            if out[-1]["evidence"] == "gps_path":
                out[-1]["note"] = "Passed between two GPS fixes; time is the window between them."
        return out

    def _undated_hits(self, hits: list[_Hit]) -> list[dict[str, Any]]:
        best: dict[int, _Hit] = {}
        for h in hits:
            if h.a.file_id not in best or h.distance_m < best[h.a.file_id].distance_m:
                best[h.a.file_id] = h
        files = self._files(best)
        out = []
        for fid, h in best.items():
            f = files[fid]
            out.append({
                "evidence": "planned_route" if h.a.kind == "route" else "track",
                "trip": f["folder_name"] or f["name"], "file": f["name"],
                "dates": _span(f["date_from"], f["date_to"]), "dates_from": f["date_source"],
                "closest_m": round(h.distance_m),
            })
        return sorted(out, key=lambda x: x["dates"] or "")

    def _waypoint_hits(self, place: Place, s: date | None, e: date | None) -> list[dict[str, Any]]:
        where, args = "f.category = 'trip'", []
        if s or e:
            where += " AND f.date_from <= ? AND f.date_to >= ?"
            args += [(e or date(2999, 1, 1)).isoformat(), (s or date(1900, 1, 1)).isoformat()]
        box = place.shape.bbox().expand(place.tolerance_m + 500)
        out = []
        for r in self.index.db.execute(
                "SELECT w.name, w.lon, w.lat, f.name AS file, f.date_from, f.date_to, f.date_source, d.name AS trip "
                f"FROM waypoints w JOIN files f ON f.id=w.file_id LEFT JOIN folders d ON d.path=f.folder WHERE {where}",
                args):
            if not box.contains((r["lon"], r["lat"])):
                continue
            dist = place.shape.distance_m((r["lon"], r["lat"]))
            if dist <= max(place.tolerance_m, 300 if place.kind != "area" else 0):
                out.append({"evidence": "marked_place", "name": r["name"], "trip": r["trip"] or r["file"],
                            "dates": _span(r["date_from"], r["date_to"]), "closest_m": round(dist)})
        return out

    async def where_was_i(self, day: str) -> dict[str, Any]:
        deadline = self._deadline()
        d = parse_day(day, "date")
        if d is None:
            raise ToolError("Say which day (2026-11-08).")
        note = await self.ensure_fresh()
        rows = self.index.db.execute(
            "SELECT DISTINCT p.ts, p.lon, p.lat, p.tz, p.accuracy FROM points p JOIN files f ON f.id=p.file_id "
            "WHERE f.category IN ('daily','trip') AND p.ts IS NOT NULL AND p.local_date = ? ORDER BY p.ts",
            (d.isoformat(),)).fetchall()
        pts = [r for r in rows if r["accuracy"] is None or r["accuracy"] <= MAX_ACCURACY_M]
        out: dict[str, Any] = {"date": d.isoformat()}
        trips = self._trips_on(d)
        if trips:
            out["trips_that_day"] = trips
        await self._photo_summary(out, self.photos.on_day(d, deadline) if self.photos else None)
        if not pts:
            out["gps_data"] = False
            out["hint"] = ("The phone recorded no GPS that day; 'photos' says where pictures were taken."
                           if out.get("photos") else "The phone recorded nothing that day and there are no photos.")
            out.update(note)
            return out
        out["gps_data"] = True
        out["time_zones"] = sorted({r["tz"] for r in pts})
        out["first_fix"], out["last_fix"] = _hhmm(pts[0]["ts"], pts[0]["tz"]), _hhmm(pts[-1]["ts"], pts[-1]["tz"])
        # stops: consecutive fixes within STOP_RADIUS_M of the first one for >= STOP_MIN_S
        stops, i = [], 0
        while i < len(pts):
            j = i
            while j + 1 < len(pts) and haversine_m((pts[i]["lon"], pts[i]["lat"]),
                                                   (pts[j + 1]["lon"], pts[j + 1]["lat"])) <= STOP_RADIUS_M:
                j += 1
            if pts[j]["ts"] - pts[i]["ts"] >= STOP_MIN_S:
                stops.append((pts[i], pts[j]))
            i = j + 1
        budget = MAX_REVERSE_PER_CALL
        stop_out = []
        known: list[tuple[tuple[float, float], str]] = []  # (position, locality) already looked up
        for a, b in stops[:8]:
            if budget <= 0 or not self._time_left(deadline):
                stop_out.append({"from": _hhmm(a["ts"], a["tz"]), "to": _hhmm(b["ts"], b["tz"]), "place": None})
                continue
            rev = await self.geo.reverse(a["lon"], a["lat"], 17)
            budget -= 1
            if (loc := locality_of(rev)):
                known.append(((a["lon"], a["lat"]), loc))
            stop_out.append({"from": _hhmm(a["ts"], a["tz"]), "to": _hhmm(b["ts"], b["tz"]), "place": street_of(rev)})
        out["stops"] = stop_out
        if len(stops) > len(stop_out):
            out["more_stops"] = len(stops) - len(stop_out)
        # localities passed, in order: one lookup per DAY_SAMPLE_M travelled
        localities: list[dict[str, str]] = []
        last = None
        for r in pts:
            pos = (r["lon"], r["lat"])
            if last and haversine_m(last, pos) < DAY_SAMPLE_M:
                continue
            last = pos
            name = next((n for q, n in known if haversine_m(q, pos) < 1500), None)
            if name is None:
                if budget <= 0 or not self._time_left(deadline):
                    out["localities_truncated"] = True
                    break
                name = locality_of(await self.geo.reverse(r["lon"], r["lat"], 10))
                budget -= 1
                if name:
                    known.append((pos, name))
            if name and (not localities or localities[-1]["place"] != name):
                localities.append({"place": name, "time": _hhmm(r["ts"], r["tz"])})
        out["localities"] = localities
        out.update(note)
        return out

    def _trips_on(self, d: date) -> list[str]:
        iso = d.isoformat()
        rows = self.index.db.execute(
            "SELECT DISTINCT name FROM folders WHERE category='trip' AND date_from <= ? AND date_to >= ? ORDER BY name",
            (iso, iso)).fetchall()
        return [r[0] for r in rows]

    def _trip_rows(self) -> list[sqlite3.Row]:
        return self.index.db.execute(
            "SELECT d.path, d.name, d.date_from, d.date_to, d.explicit_range, "
            " COUNT(f.id) AS files, COALESCE(SUM(f.n_timed),0) AS timed, COALESCE(SUM(f.n_points),0) AS points, "
            " COALESCE(SUM(f.n_waypoints),0) AS waypoints "
            "FROM folders d LEFT JOIN files f ON f.folder = d.path "
            "WHERE d.category='trip' GROUP BY d.path ORDER BY d.date_from, d.name").fetchall()

    def _trip_extent(self, folder: str) -> tuple[str | None, str | None]:
        r = self.index.db.execute(
            "SELECT MIN(p.local_date), MAX(p.local_date) FROM points p JOIN files f ON f.id=p.file_id "
            "WHERE f.folder=? AND p.ts IS NOT NULL", (folder,)).fetchone()
        return r[0], r[1]

    async def list_trips(self, year: int | None, query: str | None) -> dict[str, Any]:
        note = await self.ensure_fresh()
        trips = []
        q_words = words(query or "") - {"de", "del", "la", "el", "en", "viaje", "trip"}
        for r in self._trip_rows():
            if r["files"] == 0:
                continue
            if year and not (r["date_from"] and r["date_from"][:4] <= str(year) <= r["date_to"][:4]):
                continue
            if q_words:
                names = " ".join([r["name"]] + [w[0] or "" for w in self.index.db.execute(
                    "SELECT w.name FROM waypoints w JOIN files f ON f.id=w.file_id WHERE f.folder=?", (r["path"],))])
                if not q_words <= words(names) and normalize(query or "") not in normalize(names):
                    continue
            trips.append(self._trip_brief(r))
        out: dict[str, Any] = {"count": len(trips), "trips": trips[:60]}
        if not trips and (year or query):
            out["hint"] = "No trip folder matches; loc_was_i_at also searches the daily GPS history."
        out.update(note)
        return out

    def _trip_brief(self, r: sqlite3.Row) -> dict[str, Any]:
        content = []
        if r["timed"]:
            content.append("gps")
        if r["points"] - r["timed"]:
            content.append("routes/tracks")
        if r["waypoints"]:
            content.append("marked places")
        return {"trip": r["name"], "dates": _span(r["date_from"], r["date_to"]),
                "dates_are_range": bool(r["explicit_range"]), "files": r["files"], "content": content}

    async def trip_summary(self, trip: str) -> dict[str, Any]:
        deadline = self._deadline()
        note = await self.ensure_fresh()
        rows = [r for r in self._trip_rows() if r["files"]]
        r = resolve(trip, rows, names=lambda x: [x["name"], re.sub(r"^[\d_-]+", "", x["name"])],
                    label=lambda x: x["name"], kind="trip")
        out = self._trip_brief(r)
        first, last = self._trip_extent(r["path"])
        if first:
            out["gps_days"] = _span(first, last)
        files = self.index.db.execute(
            "SELECT id, name, date_from, date_to, date_source, n_points, n_timed, n_waypoints, error "
            "FROM files WHERE folder=? ORDER BY date_from, name", (r["path"],)).fetchall()
        out["files_detail"] = [{
            "file": f["name"], "dates": _span(f["date_from"], f["date_to"]), "dates_from": f["date_source"],
            "content": ("gps" if f["n_timed"] else "route/track" if f["n_points"] else "marked places"
                        if f["n_waypoints"] else "empty") if not f["error"] else "unreadable",
        } for f in files[:40]]
        marked = [w[0] for w in self.index.db.execute(
            "SELECT DISTINCT w.name FROM waypoints w JOIN files f ON f.id=w.file_id WHERE f.folder=? AND w.name != ''",
            (r["path"],))]
        if marked:
            out["marked_places"] = marked[:60]
        if self.photos and r["date_from"] and r["date_to"]:
            await self._photo_summary(out, self.photos.in_range(date.fromisoformat(r["date_from"]),
                                                                date.fromisoformat(r["date_to"]), deadline))
        # localities along the trip: fixes/vertices at least ~10 km apart
        pts = self.index.db.execute(
            "SELECT p.lon, p.lat FROM points p JOIN files f ON f.id=p.file_id WHERE f.folder=? "
            "ORDER BY f.date_from, f.name, p.seg, p.seq", (r["path"],)).fetchall()
        localities: list[str] = []
        last_pt = None
        budget = MAX_REVERSE_PER_CALL
        for p in pts:
            if budget <= 0 or not self._time_left(deadline):
                out["localities_truncated"] = True
                break
            if last_pt and haversine_m(last_pt, (p["lon"], p["lat"])) < TRIP_SAMPLE_M:
                continue
            last_pt = (p["lon"], p["lat"])
            name = locality_of(await self.geo.reverse(p["lon"], p["lat"], 10))
            budget -= 1
            if name and name not in localities:
                localities.append(name)
        if localities:
            out["localities"] = localities
        out.update(note)
        return out

    async def runs_near(self, place: str, radius_m: int, start: str | None, end: str | None) -> dict[str, Any]:
        s, e = parse_day(start, "start"), parse_day(end, "end")
        note = await self.ensure_fresh()
        candidates = await self.resolve_place(place)
        pts = self._points("1=1", (), ("sport",))
        files = self._files(p.file_id for p in pts)
        by_file: dict[int, list[_Pt]] = {}
        for p in pts:
            by_file.setdefault(p.file_id, []).append(p)
        chosen, runs = candidates[0], []
        for cand in candidates:
            near = Place(cand.name, cand.shape, cand.kind, cand.tolerance_m + radius_m)
            runs = []
            for fid, fpts in by_file.items():
                hits = self._hits(near, fpts)
                if not hits:
                    continue
                timed = [p for p in fpts if p.ts]
                f = files[fid]
                day = timed[0].local_date if timed else f["date_from"]
                if (s and (not day or day < s.isoformat())) or (e and (not day or day > e.isoformat())):
                    continue
                length = sum(haversine_m((a.lon, a.lat), (b.lon, b.lat)) for a, b in zip(fpts, fpts[1:])
                             if a.seg == b.seg)
                run: dict[str, Any] = {"date": day, "closest_m": round(min(h.distance_m for h in hits)),
                                       "distance_km": round(length / 1000, 1), "file": f["name"]}
                if timed:
                    run["start_time"] = timed[0].local_time
                    run["duration_min"] = round((timed[-1].ts - timed[0].ts) / 60)  # type: ignore[operator]
                    run["time_zone"] = timed[0].tz
                elif t := tracks.filename_time(f["name"]):
                    run["start_time"] = t
                    run["note"] = "Date and time from the file name (the recording has no timestamps)."
                runs.append(run)
            if runs:
                chosen = cand
                break
        runs.sort(key=lambda r: r["date"] or "")
        out = {"place": chosen.name, "radius_m": radius_m, "count": len(runs), "runs": runs[:MAX_RESULTS]}
        if runs:
            out["first"], out["last"] = runs[0]["date"], runs[-1]["date"]
        out.update(note)
        return out

    async def chargers_near(self, place: str, radius_km: float, min_kw: int | None) -> dict[str, Any]:
        note = await self.ensure_fresh()
        cand = (await self.resolve_place(place))[0]
        center = cand.shape.bbox().center if cand.kind != "point" else cand.shape.points[0]
        found = []
        for r in self.index.db.execute(
                "SELECT w.name, w.descr, w.type, w.lon, w.lat FROM waypoints w JOIN files f ON f.id=w.file_id "
                "WHERE f.category='chargers'"):
            pos = (r["lon"], r["lat"])
            dist = cand.shape.distance_m(pos) if cand.kind != "point" else haversine_m(pos, center)
            if dist > radius_km * 1000:
                continue
            kw = _power_kw(" ".join(x or "" for x in (r["descr"], r["type"], r["name"])))
            if min_kw and (kw is None or kw < min_kw):
                continue
            found.append({"name": r["name"], "type": r["type"] or None, "max_kw": kw,
                          "distance_km": round(haversine_m(pos, center) / 1000, 1),
                          "inside_place": dist == 0 and cand.kind == "area",
                          "details": (r["descr"] or "")[:240] or None})
        found.sort(key=lambda x: x["distance_km"])
        out = {"place": cand.name, "radius_km": radius_km, "count": len(found), "chargers": found[:15]}
        if not found:
            out["hint"] = "None of the saved chargers is that close; try a larger radius."
        out.update(note)
        return out


def _drop_foreign_times(parsed: tracks.Parsed, folder: tracks.DateRange) -> None:
    """In a dated trip folder, a timestamp from another year is an export or
    converter artefact, not when the user was there: keep the point untimed."""
    years = {folder[0].year, folder[1].year}
    for p in parsed.points:
        if p.ts and p.ts.year not in years:
            p.ts = None
    for w in parsed.waypoints:
        if w.ts and w.ts.year not in years:
            w.ts = None


def _day_ranges(days: list[str]) -> list[str]:
    """['2011-08-01', '2011-08-02', '2011-08-05'] -> ['2011-08-01 to 2011-08-02', '2011-08-05']"""
    out: list[list[date]] = []
    for d in sorted(date.fromisoformat(x) for x in days):
        if out and d - out[-1][1] == timedelta(days=1):
            out[-1][1] = d
        else:
            out.append([d, d])
    return [_span(a.isoformat(), b.isoformat()) or "" for a, b in out]


def _span(a: str | None, b: str | None) -> str | None:
    if not a:
        return None
    return a if not b or a == b else f"{a} to {b}"


_KW_RE = re.compile(r"(\d{2,3})\s*kW", re.I)


def _power_kw(text: str) -> int | None:
    m = re.search(r"m[aá]xima[^.]*?(\d{2,3})\s*kW", text, re.I)
    if m:
        return int(m.group(1))
    values = [int(v) for v in _KW_RE.findall(text)]
    return max(values) if values else None


# ---------------------------------------------------------------- MCP tools

_DATE_DOC = "Day as YYYY-MM-DD (local day where the user was)."


def register(server: MCPServer, loc: Locations) -> None:
    @server.tool(
        name="loc_was_i_at",
        description=(
            "Whether the user's phone location history and the family photo library show them at a place (city, "
            "town, street, landmark) on a day, in a period, or ever. Use for '¿pasé por la Gran Vía el 10 de "
            "noviembre?', '¿estuve en Segovia el 8?', '¿cuándo estuve en Oviedo?'. Add the town to streets ('Gran "
            "Vía, Madrid'). Times are local to where the user was. Report the 'evidence': 'gps'/'gps_path' (visits) "
            "and 'photo' (photo_days, a picture taken there; lists who appears in them) show presence; "
            "'planned_route' and 'marked_place' do not. With 'person' ('¿estuvo Carmen en Llanes?') only photos "
            "where that recognised person appears count."
        ),
        annotations=_READ_ONLY,
    )
    async def loc_was_i_at(
        place: Annotated[str, Field(description="Place name, e.g. 'Segovia' or 'Gran Vía, Madrid'.", max_length=200)],
        date: Annotated[str | None, Field(description=_DATE_DOC, max_length=10)] = None,
        start: Annotated[str | None, Field(description="First day of a period (YYYY-MM-DD).", max_length=10)] = None,
        end: Annotated[str | None, Field(description="Last day of a period (YYYY-MM-DD).", max_length=10)] = None,
        person: Annotated[str | None, Field(description="Someone else, as recognised in the photo library "
                                            "(fuzzy-matched). Omit for the user.", max_length=60)] = None,
    ) -> dict[str, Any]:
        return await loc.was_i_at(place, date, start, end, person)

    @server.tool(
        name="loc_where_was_i",
        description=("Where the user was on a given day from the phone location history: localities in order, "
                     "longer stops with times, any trip that day, and the photos taken that day (count, times, "
                     "places, people), also on days without GPS. Use for '¿dónde estuve el sábado?'."),
        annotations=_READ_ONLY,
    )
    async def loc_where_was_i(
        date: Annotated[str, Field(description=_DATE_DOC, max_length=10)],
    ) -> dict[str, Any]:
        return await loc.where_was_i(date)

    @server.tool(
        name="loc_list_trips",
        description=("Trips the user saved (one folder per trip, with dates; a range like 7-14 means the whole "
                     "period). Optional year and place/name filter. Use for '¿qué viajes hicimos en 2014?'."),
        annotations=_READ_ONLY,
    )
    async def loc_list_trips(
        year: Annotated[int | None, Field(description="Only trips in this year.", ge=1990, le=2100)] = None,
        query: Annotated[str | None, Field(description="Words in the trip or its places, e.g. 'Sicilia'.",
                                           max_length=80)] = None,
    ) -> dict[str, Any]:
        return await loc.list_trips(year, query)

    @server.tool(
        name="loc_trip_summary",
        description=("Details of one saved trip: dates, localities along the way, places marked, files, and the "
                     "photos taken during it (per day, places, people). "
                     "Use for '¿por dónde pasamos en el viaje a Galicia?'."),
        annotations=_READ_ONLY,
    )
    async def loc_trip_summary(
        trip: Annotated[str, Field(description="Trip name as in loc_list_trips, or part of it.", max_length=120)],
    ) -> dict[str, Any]:
        return await loc.trip_summary(trip)

    @server.tool(
        name="loc_runs_near",
        description=("Runs/workouts recorded with Sports Tracker that passed near a place, with date, start time, "
                     "duration and length. Use for '¿he corrido alguna vez por el Retiro?' and '¿cuándo fue?'."),
        annotations=_READ_ONLY,
    )
    async def loc_runs_near(
        place: Annotated[str, Field(description="Place name, e.g. 'Parque del Retiro, Madrid'.", max_length=200)],
        radius_m: Annotated[int, Field(description="How near counts, in metres.", ge=0, le=20000)] = 300,
        start: Annotated[str | None, Field(description="Only from this day (YYYY-MM-DD).", max_length=10)] = None,
        end: Annotated[str | None, Field(description="Only until this day (YYYY-MM-DD).", max_length=10)] = None,
    ) -> dict[str, Any]:
        return await loc.runs_near(place, radius_m, start, end)

    @server.tool(
        name="loc_chargers_near",
        description=("EV fast chargers from the user's saved list near a place, nearest first, with power. "
                     "Use for '¿qué cargadores hay cerca de Mérida?'."),
        annotations=_READ_ONLY,
    )
    async def loc_chargers_near(
        place: Annotated[str, Field(description="Place name, e.g. 'Mérida' or 'A-5 Almaraz'.", max_length=200)],
        radius_km: Annotated[float, Field(description="Search radius in km.", gt=0, le=300)] = 25,
        min_kw: Annotated[int | None, Field(description="Only chargers with at least this power (kW).",
                                            ge=1, le=1000)] = None,
    ) -> dict[str, Any]:
        return await loc.chargers_near(place, radius_km, min_kw)
