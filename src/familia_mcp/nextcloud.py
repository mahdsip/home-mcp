"""Nextcloud module: calendar, tasks and contacts over CalDAV / CardDAV.

What the household asks by voice: "¿qué tengo mañana?", "añade comprar leche
a la lista de la compra", "marca como hecha llamar al fontanero", "apúntame
dentista el jueves a las cinco en la calle Mayor", "¿cuál es el teléfono de
Ana?", "guarda el número de Pedro, el fontanero".

Writes are limited by construction:
  - the client only sends PROPFIND, REPORT and PUT (never DELETE, MOVE,
    MKCALENDAR, PROPPATCH...), and only below the user's own calendar and
    address book homes;
  - a PUT either creates a new object (If-None-Match: *) or replaces the
    exact version it just read (If-Match: <etag>), so it never overwrites a
    change made meanwhile on a phone;
  - appointments and contacts are parked and only written after the user's
    spoken "yes" (same out-of-band confirmation as Home Assistant: the proxy
    calls the internal `nc_confirm_action`); tasks are written directly;
  - events never get attendees, so Nextcloud never emails invitations.

Nextcloud app passwords cannot be scoped to calendars/contacts: the checks
above are the boundary. Use a dedicated app password ("home-mcp") so it can
be revoked on its own.
"""

from __future__ import annotations

import logging
import secrets
import ssl
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Awaitable, Callable, Literal
from urllib.parse import quote, unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from icalendar import Alarm, Calendar, Event, Todo
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from . import vcard
from .config import NextcloudConfig
from .homeassistant import INTERNAL_META
from .matching import STOPWORDS, normalize, resolve, words

logger = logging.getLogger("familia_mcp.nextcloud")

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "card": "urn:ietf:params:xml:ns:carddav",
      "cs": "http://calendarserver.org/ns/"}
PRODID = "-//home-mcp//familIA//EN"

MAX_EVENTS = 30
MAX_TASKS = 30
MAX_CONTACT_MATCHES = 5
MAX_AGENDA_DAYS = 31
TEXT_CHARS = 200
TITLE_CHARS = 100  # task titles are read aloud; the full one stays in Nextcloud
_ALLOWED_METHODS = frozenset({"PROPFIND", "REPORT", "PUT"})
# Collections Nextcloud generates itself: never written, never listed for tasks/events input.
_SKIP_CALENDARS = ("inbox", "outbox", "trashbin")
_SYSTEM_ADDRESSBOOK_PREFIXES = ("z-server-generated--", "z-app-generated--")

Priority = Literal["high", "medium", "low"]
_PRIORITY = {"high": 1, "medium": 5, "low": 9}
TaskFilter = Literal["open", "due_today", "overdue", "upcoming_week"]
Freq = Literal["none", "daily", "weekly", "monthly", "yearly"]
PhoneType = Literal["cell", "home", "work", "other"]
EmailType = Literal["home", "work", "other"]


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _short(text: Any, limit: int = TEXT_CHARS) -> str | None:
    if not text:
        return None
    s = " ".join(str(text).split())
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


def _utc_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _tag(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


# --------------------------------------------------------------------------
# DAV client
# --------------------------------------------------------------------------
@dataclass
class DavResource:
    href: str
    props: dict[str, ET.Element] = field(default_factory=dict)

    def text(self, prefix: str, name: str) -> str | None:
        el = self.props.get(_tag(prefix, name))
        return el.text if el is not None and el.text is not None else None


def parse_multistatus(body: bytes) -> list[DavResource]:
    # Responses come from the household's own Nextcloud; Python's expat
    # rejects entity-expansion attacks anyway.
    root = ET.fromstring(body)
    out = []
    for resp in root.findall("d:response", NS):
        href = unquote((resp.findtext("d:href", default="", namespaces=NS) or "").strip())
        res = DavResource(href)
        for ps in resp.findall("d:propstat", NS):
            status = ps.findtext("d:status", default="", namespaces=NS)
            if " 200 " not in f"{status} ":
                continue
            prop = ps.find("d:prop", NS)
            for child in list(prop) if prop is not None else []:
                res.props[child.tag] = child
        out.append(res)
    return out


def ssl_context(cert_file: str | None) -> ssl.SSLContext | bool:
    """Pinned self-signed certificate: trust exactly that certificate. The
    hostname check is off because the cert names the box (e.g. 'nextcloudpi') while
    we connect by IP; the pin is what authenticates the server, and it is
    verified in the handshake, before any credential is sent."""
    if not cert_file:
        return True
    ctx = ssl.create_default_context(cafile=cert_file)
    ctx.check_hostname = False
    ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN  # self-signed leaf as trust anchor
    return ctx


class DavClient:
    def __init__(self, cfg: NextcloudConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.user = cfg.user
        self.base = urlsplit(cfg.url)
        u = quote(cfg.user, safe="")
        self.calendar_home = f"/remote.php/dav/calendars/{u}/"
        self.addressbook_home = f"/remote.php/dav/addressbooks/users/{u}/"
        self._http = httpx.AsyncClient(
            base_url=cfg.url,
            auth=(cfg.user, cfg.app_password),
            timeout=cfg.timeout_s,
            verify=ssl_context(cfg.cert_file),
            headers={"User-Agent": "home-mcp"},
            transport=transport,
            follow_redirects=False,
        )

    def _path(self, href: str) -> str:
        """Server-relative path of an href, refused unless it is under one of
        the user's homes (no '..', no other host)."""
        parts = urlsplit(urljoin(self.calendar_home, href))
        if parts.netloc and parts.netloc != self.base.netloc:
            raise ToolError("Nextcloud returned a link to another server.")
        path = parts.path
        if ".." in path.split("/") or not path.startswith((self.calendar_home, self.addressbook_home)):
            raise ToolError("Nextcloud path not allowed.")  # programming error or tampered response
        return quote(unquote(path), safe="/@:+-._~")

    async def request(self, method: str, href: str, body: str | None = None,
                      headers: dict[str, str] | None = None) -> httpx.Response:
        if method not in _ALLOWED_METHODS:
            raise ToolError(f"Nextcloud method not allowed: {method}")
        path = self._path(href)
        hdrs = dict(headers or {})
        if method == "PUT" and not (hdrs.get("If-None-Match") == "*" or hdrs.get("If-Match")):
            raise ToolError("Refusing an unconditional PUT.")  # create-only or replace-exact-version
        if body is not None and "Content-Type" not in hdrs:
            hdrs["Content-Type"] = "application/xml; charset=utf-8"
        try:
            resp = await self._http.request(method, path, content=body.encode("utf-8") if body else None,
                                            headers=hdrs)
        except httpx.HTTPError as exc:
            raise ToolError(f"Nextcloud is not reachable ({type(exc).__name__}).") from exc
        if resp.status_code == 401:
            raise ToolError("Nextcloud rejected the user or app password.")
        if resp.status_code == 412:
            raise ToolError("The item changed on another device meanwhile; ask again.")
        if resp.status_code >= 400:
            raise ToolError(f"Nextcloud returned HTTP {resp.status_code}.")
        return resp

    async def propfind(self, href: str, props: str, depth: str = "1") -> list[DavResource]:
        body = f'<?xml version="1.0"?><d:propfind {_xmlns()}><d:prop>{props}</d:prop></d:propfind>'
        resp = await self.request("PROPFIND", href, body, {"Depth": depth})
        return parse_multistatus(resp.content)

    async def report(self, href: str, body: str) -> list[DavResource]:
        resp = await self.request("REPORT", href, body, {"Depth": "1"})
        return parse_multistatus(resp.content)

    async def put(self, href: str, data: str, content_type: str, *, etag: str | None = None) -> None:
        headers = {"Content-Type": f"{content_type}; charset=utf-8"}
        headers.update({"If-Match": etag} if etag else {"If-None-Match": "*"})
        await self.request("PUT", href, data, headers)


def _xmlns() -> str:
    return " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())


# --------------------------------------------------------------------------
# Collections
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Collection:
    href: str
    name: str
    components: frozenset[str]  # VEVENT / VTODO (calendars); empty for address books
    writable: bool
    ctag: str | None = None     # changes whenever anything in the collection changes


def _writable(res: DavResource) -> bool:
    el = res.props.get(_tag("d", "current-user-privilege-set"))
    if el is None:
        return False
    names = {child.tag for priv in el.findall("d:privilege", NS) for child in priv}
    return bool(names & {_tag("d", "write"), _tag("d", "write-content"), _tag("d", "all")})


# --------------------------------------------------------------------------
# Parked (confirmation-pending) writes
# --------------------------------------------------------------------------
@dataclass
class _Parked:
    run: Callable[[], Awaitable[dict[str, Any]]]
    expires: float


class ParkedActions:
    def __init__(self, ttl_s: float):
        self.ttl_s = ttl_s
        self._items: dict[str, _Parked] = {}

    def park(self, run: Callable[[], Awaitable[dict[str, Any]]]) -> str:
        self._expire()
        token = secrets.token_urlsafe(12)
        self._items[token] = _Parked(run, time.monotonic() + self.ttl_s)
        return token

    async def confirm(self, token: str) -> dict[str, Any]:
        self._expire()
        item = self._items.pop(token, None)
        if item is None:
            raise ToolError("Unknown or expired confirmation.")
        return await item.run()

    def _expire(self) -> None:
        now = time.monotonic()
        for t in [t for t, p in self._items.items() if p.expires < now]:
            del self._items[t]


def _confirmation(token: str, what: str, details: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "needs_confirmation",
        # The proxy strips the token and the tool name before the model sees them.
        "confirmation": {
            "confirmation_token": token,
            "confirm_tool": "nc_confirm_action",
            "action": what,
            "details": details,
            "instructions": (
                "Read the details back to the user in one short sentence (date, time, place / name, "
                "phone) and ask yes or no. Do NOT call any tool to confirm; the user's yes/no is "
                "handled outside this conversation."
            ),
        },
    }


# --------------------------------------------------------------------------
# Domain logic
# --------------------------------------------------------------------------
class Nextcloud:
    def __init__(self, client: DavClient, cfg: NextcloudConfig, *, now: Callable[[], datetime] | None = None):
        self.client = client
        self.cfg = cfg
        try:
            self.tz = ZoneInfo(cfg.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown time zone '{cfg.timezone}'") from exc
        self._now = now or (lambda: datetime.now(self.tz))
        self.parked = ParkedActions(cfg.confirm_ttl_s)
        # address book href -> (ctag, parsed contacts): the process lives as long as
        # the proxy's MCP session, so repeated lookups skip the download.
        self._contact_cache: dict[str, tuple[str, list[vcard.Contact]]] = {}

    def now(self) -> datetime:
        return self._now().astimezone(self.tz)

    # ----------------------------------------------------------- collections
    async def calendars(self) -> list[Collection]:
        props = ("<d:displayname/><d:resourcetype/><c:supported-calendar-component-set/>"
                 "<d:current-user-privilege-set/>")
        out = []
        for res in await self.client.propfind(self.client.calendar_home, props):
            rtype = res.props.get(_tag("d", "resourcetype"))
            if rtype is None or rtype.find("c:calendar", NS) is None:
                continue
            slug = res.href.rstrip("/").rsplit("/", 1)[-1]
            if slug in _SKIP_CALENDARS:
                continue
            comps_el = res.props.get(_tag("c", "supported-calendar-component-set"))
            comps = frozenset(c.get("name", "") for c in comps_el.findall("c:comp", NS)) if comps_el is not None \
                else frozenset({"VEVENT", "VTODO"})
            name = res.text("d", "displayname") or slug
            if self.cfg.calendars and name not in self.cfg.calendars:
                continue
            out.append(Collection(res.href, name, comps & {"VEVENT", "VTODO"}, _writable(res)))
        return out

    async def _calendars_for(self, component: str, *, write: bool) -> list[Collection]:
        cals = [c for c in await self.calendars() if component in c.components and (c.writable or not write)]
        if not cals:
            kind = "task lists" if component == "VTODO" else "calendars"
            raise ToolError(f"No {'writable ' if write else ''}{kind} available in Nextcloud.")
        return cals

    async def _pick(self, component: str, name: str | None, default: str | None) -> Collection:
        cals = await self._calendars_for(component, write=True)
        wanted = name or default
        if not wanted:
            # Nextcloud's own default calendar has the URI "personal"; else the first one.
            return next((c for c in cals if c.href.rstrip("/").endswith("/personal")), cals[0])
        kind = "task list" if component == "VTODO" else "calendar"
        return resolve(wanted, cals, names=lambda c: [c.name], label=lambda c: c.name, kind=kind)

    async def list_calendars(self) -> dict[str, Any]:
        cals = await self.calendars()
        return {
            "calendars": [{"name": c.name, "writable": c.writable} for c in cals if "VEVENT" in c.components],
            "task_lists": [{"name": c.name, "writable": c.writable} for c in cals if "VTODO" in c.components],
            "default_calendar": self.cfg.default_calendar,
            "default_task_list": self.cfg.default_tasklist,
        }

    # ------------------------------------------------------------ parsing
    async def _query(self, cal: Collection, component: str, extra_filter: str = "",
                     expand: tuple[datetime, datetime] | None = None) -> list[tuple[DavResource, Calendar]]:
        data = "<c:calendar-data/>"
        if expand:
            data = (f'<c:calendar-data><c:expand start="{_utc_stamp(expand[0])}" '
                    f'end="{_utc_stamp(expand[1])}"/></c:calendar-data>')
        body = (f'<?xml version="1.0"?><c:calendar-query {_xmlns()}><d:prop><d:getetag/>{data}</d:prop>'
                f'<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="{component}">{extra_filter}'
                f'</c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')
        out = []
        for res in await self.client.report(cal.href, body):
            raw = res.text("c", "calendar-data")
            if not raw:
                continue
            try:
                out.append((res, Calendar.from_ical(raw)))
            except ValueError:
                logger.warning("skipping unparsable calendar object %s", res.href)
        return out

    def _local(self, value: Any) -> datetime | date:
        if isinstance(value, datetime):
            return value.replace(tzinfo=self.tz) if value.tzinfo is None else value.astimezone(self.tz)
        return value

    def _parse_when(self, raw: str, field_name: str) -> datetime | date:
        """'2026-11-05' -> date; '2026-11-05T17:00' (local) or with an offset -> aware datetime."""
        raw = (raw or "").strip()
        try:
            if len(raw) == 10:
                return date.fromisoformat(raw)
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ToolError(f"{field_name} must look like 2026-11-05 or 2026-11-05T17:30.") from exc
        return dt.replace(tzinfo=self.tz) if dt.tzinfo is None else dt.astimezone(self.tz)

    @staticmethod
    def _fmt(value: datetime | date | None) -> str | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.isoformat(timespec="minutes")
        return value.isoformat()

    # ------------------------------------------------------------- agenda
    async def agenda(self, start: str | None, days: int, calendar: str | None) -> dict[str, Any]:
        if not 1 <= days <= MAX_AGENDA_DAYS:
            raise ToolError(f"days must be 1-{MAX_AGENDA_DAYS}.")
        first = self._parse_when(start, "start") if start else self.now().date()
        first_day = first.date() if isinstance(first, datetime) else first
        begin = datetime.combine(first_day, datetime.min.time(), self.tz)
        end = begin + timedelta(days=days)
        cals = await self._calendars_for("VEVENT", write=False)
        if calendar:
            cals = [resolve(calendar, cals, names=lambda c: [c.name], label=lambda c: c.name, kind="calendar")]
        events = []
        for cal in cals:
            for _, ical in await self._query(cal, "VEVENT", f'<c:time-range start="{_utc_stamp(begin)}" '
                                             f'end="{_utc_stamp(end)}"/>', expand=(begin, end)):
                for ev in ical.walk("VEVENT"):
                    s = self._local(ev.decoded("dtstart"))
                    e_raw = ev.get("dtend")
                    e = self._local(ev.decoded("dtend")) if e_raw is not None else None
                    all_day = not isinstance(s, datetime)
                    if all_day and isinstance(e, date) and not isinstance(e, datetime):
                        e = e - timedelta(days=1)  # DTEND of all-day events is exclusive
                        if e <= s:
                            e = None
                    events.append({k: v for k, v in {
                        "title": str(ev.get("summary", "")) or "(sin título)",
                        "start": self._fmt(s),
                        "end": self._fmt(e),
                        "all_day": all_day,
                        "location": _short(ev.get("location")),
                        "notes": _short(ev.get("description")),
                        "calendar": cal.name,
                        "recurring": True if ev.get("recurrence-id") is not None else None,
                    }.items() if v not in (None, False) or k == "all_day"})
        events.sort(key=lambda e: (e["start"][:10], not e["all_day"], e["start"]))
        return {"from": begin.date().isoformat(), "to": (end - timedelta(days=1)).date().isoformat(),
                "count": len(events), "events": events[:MAX_EVENTS]}

    # -------------------------------------------------------------- tasks
    async def _open_tasks(self, tasklist: str | None) -> list[dict[str, Any]]:
        cals = await self._calendars_for("VTODO", write=False)
        if tasklist:
            cals = [resolve(tasklist, cals, names=lambda c: [c.name], label=lambda c: c.name, kind="task list")]
        tasks = []
        flt = '<c:prop-filter name="COMPLETED"><c:is-not-defined/></c:prop-filter>'
        for cal in cals:
            for res, ical in await self._query(cal, "VTODO", flt):
                todos = list(ical.walk("VTODO"))
                if len(todos) != 1:
                    continue  # recurring task overrides: not handled, left alone
                todo = todos[0]
                if str(todo.get("status", "")).upper() in ("COMPLETED", "CANCELLED"):
                    continue
                due = todo.get("due")
                tasks.append({"todo": todo, "ical": ical, "res": res, "cal": cal,
                              "summary": str(todo.get("summary", "")) or "(sin título)",
                              "due": self._local(todo.decoded("due")) if due is not None else None})
        return tasks

    def _task_view(self, t: dict[str, Any]) -> dict[str, Any]:
        prio = int(t["todo"].get("priority", 0) or 0)
        out = {"title": _short(t["summary"], TITLE_CHARS), "list": t["cal"].name, "due": self._fmt(t["due"]),
               "priority": "high" if 1 <= prio <= 4 else "medium" if prio == 5 else "low" if prio >= 6 else None,
               "notes": _short(t["todo"].get("description"))}
        return {k: v for k, v in out.items() if v is not None}

    async def tasks(self, tasklist: str | None, which: str) -> dict[str, Any]:
        today = self.now().date()
        tasks = await self._open_tasks(tasklist)

        def due_day(t):
            d = t["due"]
            return d.date() if isinstance(d, datetime) else d

        if which == "due_today":
            tasks = [t for t in tasks if t["due"] is not None and due_day(t) == today]
        elif which == "overdue":
            tasks = [t for t in tasks if t["due"] is not None and due_day(t) < today]
        elif which == "upcoming_week":
            tasks = [t for t in tasks if t["due"] is not None and today <= due_day(t) <= today + timedelta(days=7)]
        tasks.sort(key=lambda t: (t["due"] is None, str(due_day(t) or ""), t["summary"].lower()))
        return {"filter": which, "count": len(tasks), "tasks": [self._task_view(t) for t in tasks[:MAX_TASKS]]}

    async def add_task(self, title: str, tasklist: str | None, due: str | None,
                       priority: str | None, notes: str | None) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ToolError("The task needs a title.")
        cal = await self._pick("VTODO", tasklist, self.cfg.default_tasklist)
        uid = str(uuid.uuid4())
        todo = Todo()
        now = datetime.now(timezone.utc)
        todo.add("uid", uid)
        todo.add("dtstamp", now)
        todo.add("created", now)
        todo.add("summary", title)
        todo.add("status", "NEEDS-ACTION")
        if due:
            todo.add("due", self._parse_when(due, "due"))
        if priority:
            todo.add("priority", _PRIORITY[priority])
        if notes and notes.strip():
            todo.add("description", notes.strip())
        ical = self._wrap(todo)
        await self.client.put(f"{cal.href}{uid}.ics", ical.to_ical().decode("utf-8"), "text/calendar")
        logger.info("Nextcloud task added to %s", cal.name)
        return {"status": "done", "task": title, "list": cal.name, "due": due}

    async def complete_task(self, task: str, tasklist: str | None) -> dict[str, Any]:
        tasks = await self._open_tasks(tasklist)
        if not tasks:
            raise ToolError("There are no open tasks.")
        t = resolve(task, tasks, names=lambda t: [t["summary"]], label=lambda t: t["summary"], kind="task")
        todo = t["todo"]
        now = datetime.now(timezone.utc)
        for key in ("status", "completed", "percent-complete", "last-modified", "dtstamp"):
            todo.pop(key, None)
        todo.add("status", "COMPLETED")
        todo.add("completed", now)
        todo.add("percent-complete", 100)
        todo.add("last-modified", now)
        todo.add("dtstamp", now)
        etag = t["res"].text("d", "getetag")
        if not etag:
            raise ToolError("Nextcloud did not report the task version; not changed.")
        await self.client.put(t["res"].href, t["ical"].to_ical().decode("utf-8"), "text/calendar", etag=etag)
        logger.info("Nextcloud task completed in %s", t["cal"].name)
        return {"status": "done", "task": t["summary"], "list": t["cal"].name}

    # ------------------------------------------------------------- events
    def _wrap(self, component: Event | Todo) -> Calendar:
        cal = Calendar()
        cal.add("prodid", PRODID)
        cal.add("version", "2.0")
        cal.add_component(component)
        cal.add_missing_timezones()  # VTIMEZONE for every TZID used
        return cal

    async def create_event(
        self, title: str, start: str, end: str | None, duration_minutes: int | None, all_day: bool,
        location: str | None, notes: str | None, calendar: str | None, reminders_minutes: list[int] | None,
        repeat: str, repeat_interval: int, repeat_count: int | None, repeat_until: str | None,
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ToolError("The appointment needs a title.")
        s = self._parse_when(start, "start")
        if all_day:
            s_day = s.date() if isinstance(s, datetime) else s
            e_incl = self._parse_when(end, "end") if end else s_day
            e_day = e_incl.date() if isinstance(e_incl, datetime) else e_incl
            if e_day < s_day:
                raise ToolError("The end date is before the start date.")
            dtstart, dtend = s_day, e_day + timedelta(days=1)  # exclusive end
        else:
            if not isinstance(s, datetime):
                raise ToolError("Give a start time (2026-11-05T17:30) or set all_day.")
            if end:
                e = self._parse_when(end, "end")
                if not isinstance(e, datetime):
                    raise ToolError("end must include a time when the appointment is not all-day.")
            else:
                e = s + timedelta(minutes=duration_minutes or 60)
            if e <= s:
                raise ToolError("The end is not after the start.")
            dtstart, dtend = s, e
        cal = await self._pick("VEVENT", calendar, self.cfg.default_calendar)

        rrule: dict[str, Any] | None = None
        if repeat != "none":
            rrule = {"freq": repeat}
            if repeat_interval and repeat_interval > 1:
                rrule["interval"] = repeat_interval
            if repeat_count:
                rrule["count"] = repeat_count
            elif repeat_until:
                until = self._parse_when(repeat_until, "repeat_until")
                until_day = until.date() if isinstance(until, datetime) else until
                rrule["until"] = until_day if all_day else datetime.combine(
                    until_day, datetime.max.time().replace(microsecond=0), self.tz).astimezone(timezone.utc)
        reminders = sorted({int(m) for m in reminders_minutes or []})

        details = {k: v for k, v in {
            "title": title,
            "calendar": cal.name,
            "start": self._fmt(dtstart),
            "end": self._fmt(dtend - timedelta(days=1)) if all_day else self._fmt(dtend),
            "all_day": all_day,
            "location": (location or "").strip() or None,
            "notes": (notes or "").strip() or None,
            "reminders_minutes_before": reminders or None,
            "repeat": ({"every": repeat_interval or 1, "freq": repeat,
                        "count": repeat_count, "until": repeat_until} if rrule else None),
        }.items() if v is not None}

        async def run() -> dict[str, Any]:
            uid = str(uuid.uuid4())
            ev = Event()
            now = datetime.now(timezone.utc)
            ev.add("uid", uid)
            ev.add("dtstamp", now)
            ev.add("created", now)
            ev.add("summary", title)
            ev.add("dtstart", dtstart)
            ev.add("dtend", dtend)
            if details.get("location"):
                ev.add("location", details["location"])
            if details.get("notes"):
                ev.add("description", details["notes"])
            if rrule:
                ev.add("rrule", rrule)
            for minutes in reminders:
                alarm = Alarm()
                alarm.add("action", "DISPLAY")
                alarm.add("description", title)
                alarm.add("trigger", timedelta(minutes=-minutes))
                ev.add_component(alarm)
            await self.client.put(f"{cal.href}{uid}.ics", self._wrap(ev).to_ical().decode("utf-8"),
                                  "text/calendar")
            logger.info("Nextcloud event created in %s", cal.name)
            return {"status": "done", "created": details}

        return _confirmation(self.parked.park(run), "create_appointment", details)

    # ----------------------------------------------------------- contacts
    async def _addressbooks(self) -> list[Collection]:
        props = "<d:displayname/><d:resourcetype/><d:current-user-privilege-set/><cs:getctag/>"
        out = []
        for res in await self.client.propfind(self.client.addressbook_home, props):
            rtype = res.props.get(_tag("d", "resourcetype"))
            if rtype is None or rtype.find("card:addressbook", NS) is None:
                continue
            slug = res.href.rstrip("/").rsplit("/", 1)[-1]
            if slug.startswith(_SYSTEM_ADDRESSBOOK_PREFIXES):
                continue
            out.append(Collection(res.href, res.text("d", "displayname") or slug, frozenset(), _writable(res),
                                  res.text("cs", "getctag")))
        return out

    async def _contacts(self) -> list[tuple[vcard.Contact, str]]:
        # Partial retrieval (RFC 6352 8.4): only the fields we use. Without it every
        # embedded PHOTO is downloaded and a lookup takes seconds on a Pi.
        fields = "".join(f'<card:prop name="{p}"/>' for p in vcard.FIELDS)
        body = (f'<?xml version="1.0"?><card:addressbook-query {_xmlns()}><d:prop><d:getetag/>'
                f'<card:address-data>{fields}</card:address-data></d:prop><card:filter test="anyof">'
                '<card:prop-filter name="FN"/></card:filter></card:addressbook-query>')
        out = []
        for book in await self._addressbooks():
            cached = self._contact_cache.get(book.href)
            if book.ctag and cached and cached[0] == book.ctag:
                contacts = cached[1]  # unchanged since last time: no download
            else:
                contacts = []
                for res in await self.client.report(book.href, body):
                    raw = res.text("card", "address-data")
                    c = vcard.parse(raw) if raw else None
                    if c:
                        contacts.append(c)
                if book.ctag:
                    self._contact_cache[book.href] = (book.ctag, contacts)
            out.extend((c, book.name) for c in contacts)
        return out

    @staticmethod
    def _match(query: str, contacts: list[tuple[vcard.Contact, str]]) -> list[tuple[vcard.Contact, str]]:
        q_skel, q_words = normalize(query), words(query) - STOPWORDS
        if not q_skel:
            raise ToolError("Contact name is empty.")
        def level(n: str) -> int:
            """1 exact, 2 every spoken word is in the name, 3 substring; 0 none."""
            skel = normalize(n)
            if skel == q_skel:
                return 1
            if q_words and q_words <= words(n):
                return 2
            if min(len(skel), len(q_skel)) >= 3 and q_skel in skel:
                return 3
            return 0

        levels: dict[int, list] = {1: [], 2: [], 3: []}
        for c, book in contacts:
            found = [lv for n in [c.name, c.organization or ""] if n and (lv := level(n))]
            if found:
                levels[min(found)].append((c, book))
        for lvl in (1, 2, 3):
            if levels[lvl]:
                return levels[lvl]
        return []

    async def find_contact(self, name: str) -> dict[str, Any]:
        contacts = await self._contacts()
        found = self._match(name, contacts)
        return {"query": name, "count": len(found),
                "contacts": [{**c.as_dict(), "address_book": b} for c, b in found[:MAX_CONTACT_MATCHES]]}

    async def add_contact(self, name: str, phones: list[dict[str, str]] | None, emails: list[dict[str, str]] | None,
                          birthday: str | None, organization: str | None, address: str | None,
                          notes: str | None) -> dict[str, Any]:
        name = " ".join((name or "").split())
        if not name:
            raise ToolError("The contact needs a name.")
        bday = None
        if birthday:
            b = birthday.strip()
            if len(b) == 5 and b[2] == "-":  # MM-DD (no year)
                b = f"--{b}"
            bday = vcard.parse_birthday(b)
            if not bday:
                raise ToolError("birthday must look like 1980-05-31 or 05-31 (no year).")
        books = [b for b in await self._addressbooks() if b.writable]
        book = next((b for b in books if b.href.rstrip("/").rsplit("/", 1)[-1] == self.cfg.addressbook), None)
        if book is None:
            raise ToolError(f"Address book '{self.cfg.addressbook}' not found or not writable.")
        contact = vcard.Contact(
            name=name,
            phones=[{"number": " ".join(p["number"].split()), **({"type": p["type"]} if p.get("type") else {})}
                    for p in phones or [] if (p.get("number") or "").strip()],
            emails=[{"email": e["email"].strip(), **({"type": e["type"]} if e.get("type") else {})}
                    for e in emails or [] if (e.get("email") or "").strip()],
            birthday=bday,
            organization=(organization or "").strip() or None,
            address=(address or "").strip() or None,
            note=(notes or "").strip() or None,
        )
        if not (contact.phones or contact.emails or contact.birthday or contact.address):
            raise ToolError("Give at least a phone, an email, a birthday or an address.")
        existing = [c.name for c, _ in await self._contacts() if normalize(c.name) == normalize(name)]
        details = {**contact.as_dict(), "address_book": book.name}
        if existing:
            details["warning"] = f"A contact named '{existing[0]}' already exists; this adds another one."

        async def run() -> dict[str, Any]:
            contact.uid = str(uuid.uuid4())
            await self.client.put(f"{book.href}{contact.uid}.vcf", vcard.build(contact, PRODID), "text/vcard")
            logger.info("Nextcloud contact added to %s", book.name)
            return {"status": "done", "created": contact.as_dict()}

        return _confirmation(self.parked.park(run), "add_contact", details)


# --------------------------------------------------------------------------
# MCP tool registration
# --------------------------------------------------------------------------
_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False)

DateArg = Annotated[str, Field(description="Local date or date-time: 2026-11-05 or 2026-11-05T17:30.", max_length=40)]


def register(server: MCPServer, nc: Nextcloud) -> None:
    @server.tool(
        name="nc_calendars",
        description="Calendars and task lists in Nextcloud (names to use in the other nc_* tools) and the defaults.",
        annotations=_READ_ONLY,
    )
    async def nc_calendars() -> dict[str, Any]:
        return await nc.list_calendars()

    @server.tool(
        name="nc_agenda",
        description=(
            "Appointments in the Nextcloud calendars for a day or a range of days (recurring ones expanded), "
            "with time, place and notes. Use for 'what do I have tomorrow?', 'what's on this week?'."
        ),
        annotations=_READ_ONLY,
    )
    async def nc_agenda(
        start: Annotated[str | None, Field(description="First day (2026-11-05); default today.", max_length=40)] = None,
        days: Annotated[int, Field(description="Number of days from start.", ge=1, le=MAX_AGENDA_DAYS)] = 1,
        calendar: Annotated[str | None, Field(description="Only this calendar.", max_length=80)] = None,
    ) -> dict[str, Any]:
        return await nc.agenda(start, days, calendar)

    @server.tool(
        name="nc_tasks",
        description="Open (not done) tasks in Nextcloud, optionally only due today, overdue or due in the next 7 days.",
        annotations=_READ_ONLY,
    )
    async def nc_tasks(
        task_list: Annotated[str | None, Field(description="Only this task list (e.g. 'Compra').", max_length=80)] = None,
        which: Annotated[TaskFilter, Field(description="Which open tasks.")] = "open",
    ) -> dict[str, Any]:
        return await nc.tasks(task_list, which)

    @server.tool(
        name="nc_add_task",
        description=(
            "Add a task (to-do) to a Nextcloud task list, e.g. 'añade leche a la lista de la compra'. "
            "Written immediately. Only call when the user asked to add it."
        ),
        annotations=_WRITE,
    )
    async def nc_add_task(
        title: Annotated[str, Field(description="What to do, as the user said it.", min_length=1, max_length=200)],
        task_list: Annotated[str | None, Field(description="Task list name; omit for the default.", max_length=80)] = None,
        due: Annotated[str | None, Field(description="Due date or date-time, local: 2026-11-05 or 2026-11-05T18:00.", max_length=40)] = None,
        priority: Annotated[Priority | None, Field(description="Only if the user said it.")] = None,
        notes: Annotated[str | None, Field(description="Extra details.", max_length=1000)] = None,
    ) -> dict[str, Any]:
        return await nc.add_task(title, task_list, due, priority, notes)

    @server.tool(
        name="nc_complete_task",
        description="Mark ONE open Nextcloud task as done, found by its name (fuzzy). Only when the user asked.",
        annotations=_WRITE,
    )
    async def nc_complete_task(
        task: Annotated[str, Field(description="Task name as the user said it.", min_length=1, max_length=200)],
        task_list: Annotated[str | None, Field(description="Task list, if the user said it.", max_length=80)] = None,
    ) -> dict[str, Any]:
        return await nc.complete_task(task, task_list)

    @server.tool(
        name="nc_create_event",
        description=(
            "Create an appointment in a Nextcloud calendar with all its details: title, start, end or duration, "
            "all-day, place, notes, reminders and repetition. Resolve relative dates ('el jueves', 'mañana a "
            "las 5') from the current date. Nothing is written yet: the result asks for a spoken yes/no. "
            "Guests cannot be invited."
        ),
        annotations=_WRITE,
    )
    async def nc_create_event(
        title: Annotated[str, Field(description="What the appointment is.", min_length=1, max_length=200)],
        start: DateArg,
        end: Annotated[str | None, Field(description="End date-time (or last day if all_day).", max_length=40)] = None,
        duration_minutes: Annotated[int | None, Field(description="Used when no end is given; default 60.", ge=5, le=1440)] = None,
        all_day: Annotated[bool, Field(description="Whole-day appointment (no time).")] = False,
        location: Annotated[str | None, Field(description="Place or address.", max_length=300)] = None,
        notes: Annotated[str | None, Field(description="Description / extra details.", max_length=2000)] = None,
        calendar: Annotated[str | None, Field(description="Calendar name; omit for the default.", max_length=80)] = None,
        reminders_minutes: Annotated[list[int] | None, Field(description="Reminders, minutes before (e.g. [30, 1440]).", max_length=3)] = None,
        repeat: Annotated[Freq, Field(description="Repetition.")] = "none",
        repeat_interval: Annotated[int, Field(description="Every N days/weeks/... (1 = every).", ge=1, le=12)] = 1,
        repeat_count: Annotated[int | None, Field(description="Number of occurrences.", ge=2, le=500)] = None,
        repeat_until: Annotated[str | None, Field(description="Last date of the repetition (2027-06-30).", max_length=40)] = None,
    ) -> dict[str, Any]:
        for m in reminders_minutes or []:
            if not 0 <= m <= 40320:
                raise ToolError("Reminders must be 0-40320 minutes before.")
        return await nc.create_event(title, start, end, duration_minutes, all_day, location, notes, calendar,
                                     reminders_minutes, repeat, repeat_interval, repeat_count, repeat_until)

    @server.tool(
        name="nc_find_contact",
        description="Look up contacts in Nextcloud by name or company: phones, emails, birthday, address.",
        annotations=_READ_ONLY,
    )
    async def nc_find_contact(
        name: Annotated[str, Field(description="Name as the user said it (fuzzy).", min_length=1, max_length=100)],
    ) -> dict[str, Any]:
        return await nc.find_contact(name)

    @server.tool(
        name="nc_add_contact",
        description=(
            "Add a contact to Nextcloud: name plus phones, emails, birthday, company, address, notes. "
            "Nothing is written yet: the result asks for a spoken yes/no (read the number back)."
        ),
        annotations=_WRITE,
    )
    async def nc_add_contact(
        name: Annotated[str, Field(description="Full name (or a descriptive name, e.g. 'Pedro fontanero').", min_length=1, max_length=120)],
        phones: Annotated[list[dict[str, str]] | None, Field(
            description="[{'number': '+34 600 000 000', 'type': 'cell|home|work|other'}]", max_length=5)] = None,
        emails: Annotated[list[dict[str, str]] | None, Field(
            description="[{'email': 'a@b.es', 'type': 'home|work|other'}]", max_length=5)] = None,
        birthday: Annotated[str | None, Field(description="1980-05-31, or 05-31 without year.", max_length=10)] = None,
        organization: Annotated[str | None, Field(description="Company.", max_length=120)] = None,
        address: Annotated[str | None, Field(description="Postal address as said.", max_length=300)] = None,
        notes: Annotated[str | None, Field(description="Notes.", max_length=1000)] = None,
    ) -> dict[str, Any]:
        for p in phones or []:
            if p.get("type") and p["type"] not in ("cell", "home", "work", "other"):
                raise ToolError("Phone type must be cell, home, work or other.")
            if len(p.get("number", "")) > 40:
                raise ToolError("Phone number too long.")
        for e in emails or []:
            if e.get("type") and e["type"] not in ("home", "work", "other"):
                raise ToolError("Email type must be home, work or other.")
            if "@" not in e.get("email", "") or len(e["email"]) > 120:
                raise ToolError("That does not look like an email address.")
        return await nc.add_contact(name, phones, emails, birthday, organization, address, notes)

    @server.tool(
        name="nc_confirm_action",
        description="INTERNAL: writes a parked appointment/contact after the user confirmed it. Never exposed to the model.",
        annotations=_WRITE,
        meta=INTERNAL_META,
    )
    async def nc_confirm_action(confirmation_token: str) -> dict[str, Any]:
        return await nc.parked.confirm(confirmation_token)
