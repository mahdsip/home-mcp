"""nextcloud module against a fake Nextcloud DAV server (httpx.MockTransport).

The fake answers PROPFIND / REPORT with multistatus XML shaped like
Nextcloud's, stores PUT bodies, and enforces If-None-Match / If-Match like
sabre/dav does (412 on mismatch).
"""

import asyncio
import re
from datetime import datetime
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import httpx
import pytest
from icalendar import Calendar
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from conftest import structured
from familia_mcp import nextcloud as ncm
from familia_mcp import vcard
from familia_mcp.config import NextcloudConfig
from familia_mcp.server import build_server

TZ = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 5, 10, 0, tzinfo=TZ)  # Thursday
USER = "ana"
CAL = f"/remote.php/dav/calendars/{USER}/"
BOOK = f"/remote.php/dav/addressbooks/users/{USER}/"


def todo(uid, summary, due=None, status="NEEDS-ACTION", completed=False, priority=None):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VTODO", f"UID:{uid}", f"SUMMARY:{summary}", f"STATUS:{status}"]
    if due:
        lines.append(f"DUE;VALUE=DATE:{due}")
    if priority:
        lines.append(f"PRIORITY:{priority}")
    if completed:
        lines.append("COMPLETED:20261101T100000Z")
    return "\r\n".join(lines + ["END:VTODO", "END:VCALENDAR", ""])


def event(uid, summary, start, end, extra=""):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            f"UID:{uid}\r\nSUMMARY:{summary}\r\nDTSTART{start}\r\nDTEND{end}\r\n{extra}END:VEVENT\r\nEND:VCALENDAR\r\n")


VCARD_ANA = ("BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c1\r\nFN:Ana García\r\nN:García;Ana;;;\r\n"
             "item1.TEL;TYPE=CELL,VOICE:+34 600 11 22 33\r\nEMAIL;TYPE=INTERNET,HOME:ana@example.org\r\n"
             "BDAY;X-APPLE-OMIT-YEAR=1604:1604-05-31\r\nEND:VCARD\r\n")
VCARD_PEDRO = ("BEGIN:VCARD\r\nVERSION:4.0\r\nUID:c2\r\nFN:Pedro Fontanero\r\nORG:Fontanería López\r\n"
               "TEL;VALUE=uri;TYPE=work:tel:+34911223344\r\nNOTE:Viene los sábados\\, mejor por la mañana\r\n"
               "END:VCARD\r\n")
VCARD_ANABEL = "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:c3\r\nFN:Anabel Ruiz\r\nTEL:+34 600 99 88 77\r\nEND:VCARD\r\n"


class FakeNextcloud:
    """Collections and objects keyed by href; each object has an etag."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.objects: dict[str, tuple[str, str]] = {
            f"{CAL}personal/t1.ics": ("e1", todo("t1", "Llamar al fontanero", due="20261103")),         # overdue
            f"{CAL}personal/t2.ics": ("e2", todo("t2", "Renovar DNI", due="20261105", priority=1)),     # today
            f"{CAL}personal/t3.ics": ("e3", todo("t3", "Pintar valla", completed=True, status="COMPLETED")),
            f"{CAL}compra/t4.ics": ("e4", todo("t4", "Comprar leche")),
            f"{CAL}compra/t5.ics": ("e5", todo("t5", "Comprar pan", due="20261110")),
            # Expanded the way sabre returns them: UTC times.
            f"{CAL}personal/ev1.ics": ("e6", event("ev1", "Dentista", ":20261105T160000Z", ":20261105T170000Z",
                                                  "LOCATION:C/ Mayor 1\\, Madrid\r\n")),
            f"{CAL}personal/ev2.ics": ("e7", event("ev2", "Puente", ";VALUE=DATE:20261105", ";VALUE=DATE:20261108")),
            f"{CAL}contact_birthdays/b1.ics": ("e8", event("b1", "Cumpleaños de Ana", ";VALUE=DATE:20261105",
                                                           ";VALUE=DATE:20261106")),
            f"{BOOK}contacts/c1.vcf": ("e9", VCARD_ANA),
            f"{BOOK}contacts/c2.vcf": ("e10", VCARD_PEDRO),
            f"{BOOK}contacts/c3.vcf": ("e11", VCARD_ANABEL),
            f"{BOOK}z-server-generated--system/s1.vcf": ("e12", "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Ana Sistema\r\n"
                                                         "TEL:1\r\nEND:VCARD\r\n"),
        }
        self.etag_counter = 100

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _ms(responses: list[str]) -> httpx.Response:
        body = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
                'xmlns:card="urn:ietf:params:xml:ns:carddav">' + "".join(responses) + "</d:multistatus>")
        return httpx.Response(207, content=body.encode(), headers={"Content-Type": "application/xml"})

    @staticmethod
    def _resp(href, props, missing=""):
        out = f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>" \
              "<d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        if missing:
            out += f"<d:propstat><d:prop>{missing}</d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
        return out + "</d:response>"

    @staticmethod
    def _priv(write):
        return ("<d:current-user-privilege-set><d:privilege><d:read/></d:privilege>"
                + ("<d:privilege><d:write/></d:privilege>" if write else "") + "</d:current-user-privilege-set>")

    def _calendar(self, slug, name, comps, write=True):
        comp = "".join(f'<c:comp name="{c}"/>' for c in comps)
        return self._resp(f"{CAL}{slug}/", f"<d:displayname>{name}</d:displayname><d:resourcetype><d:collection/>"
                          f"<c:calendar/></d:resourcetype><c:supported-calendar-component-set>{comp}"
                          f"</c:supported-calendar-component-set>{self._priv(write)}")

    def _book(self, slug, name, write=True):
        ctag = sum(1 for h in self.objects if h.startswith(f"{BOOK}{slug}/")) + self.etag_counter
        return self._resp(f"{BOOK}{slug}/", f"<d:displayname>{name}</d:displayname><d:resourcetype>"
                          f"<d:collection/><card:addressbook/></d:resourcetype>{self._priv(write)}"
                          f'<cs:getctag xmlns:cs="http://calendarserver.org/ns/">ctag-{ctag}</cs:getctag>')

    # -------------------------------------------------------------- handler
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method in ("PROPFIND", "REPORT", "PUT"), request.method
        path = request.url.path
        if request.method == "PROPFIND":
            assert request.headers["Depth"] == "1"
            if path == CAL:
                return self._ms([
                    self._resp(CAL, "<d:resourcetype><d:collection/></d:resourcetype>"),
                    self._calendar("personal", "Personal", ["VEVENT", "VTODO"]),
                    self._calendar("compra", "Compra", ["VTODO"]),
                    self._calendar("contact_birthdays", "Cumpleaños", ["VEVENT"], write=False),
                    self._calendar("inbox", "inbox", ["VEVENT"]),
                ])
            if path == BOOK:
                return self._ms([
                    self._resp(BOOK, "<d:resourcetype><d:collection/></d:resourcetype>"),
                    self._book("contacts", "Contactos"),
                    self._book("z-server-generated--system", "Usuarios", write=False),
                ])
            return httpx.Response(404)
        if request.method == "REPORT":
            body = request.content.decode()
            comp = re.findall(r'comp-filter name="(VEVENT|VTODO)"', body)
            out = []
            for href, (etag, data) in self.objects.items():
                if not href.startswith(path) or href == path:
                    continue
                if path.startswith(BOOK):
                    out.append(self._resp(href, f'<d:getetag>"{etag}"</d:getetag>'
                                          f"<card:address-data>{escape(data)}</card:address-data>"))
                    continue
                if comp and f"BEGIN:{comp[0]}" not in data:
                    continue
                if "COMPLETED" in body and "is-not-defined" in body and "\r\nCOMPLETED:" in data:
                    continue
                out.append(self._resp(href, f'<d:getetag>"{etag}"</d:getetag>'
                                      f"<c:calendar-data>{escape(data)}</c:calendar-data>"))
            return self._ms(out)
        # PUT
        current = self.objects.get(path)
        if request.headers.get("If-None-Match") == "*" and current:
            return httpx.Response(412)
        if "If-Match" in request.headers and (not current or f'"{current[0]}"' != request.headers["If-Match"]):
            return httpx.Response(412)
        self.etag_counter += 1
        self.objects[path] = (f"e{self.etag_counter}", request.content.decode())
        return httpx.Response(201 if current is None else 204)

    def puts(self):
        return [r for r in self.requests if r.method == "PUT"]


def cfg(**kw):
    base = dict(url="https://nc.example", user=USER, app_password="secret", timezone="Europe/Madrid")
    base.update(kw)
    return NextcloudConfig(**base)


@pytest.fixture
def fake():
    return FakeNextcloud()


@pytest.fixture
def nc(fake):
    c = cfg()
    return ncm.Nextcloud(ncm.DavClient(c, transport=httpx.MockTransport(fake.handler)), c, now=lambda: NOW)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- calendars
def test_list_calendars(nc):
    out = run(nc.list_calendars())
    assert out["calendars"] == [{"name": "Personal", "writable": True}, {"name": "Cumpleaños", "writable": False}]
    assert out["task_lists"] == [{"name": "Personal", "writable": True}, {"name": "Compra", "writable": True}]


def test_calendar_allowlist(fake):
    c = cfg(calendars=("Compra",))
    nc = ncm.Nextcloud(ncm.DavClient(c, transport=httpx.MockTransport(fake.handler)), c, now=lambda: NOW)
    assert run(nc.list_calendars()) == {"calendars": [], "task_lists": [{"name": "Compra", "writable": True}],
                                        "default_calendar": None, "default_task_list": None}


# ------------------------------------------------------------------ agenda
def test_agenda_today_local_times_and_all_day(nc, fake):
    out = run(nc.agenda(None, 1, None))
    assert out["from"] == out["to"] == "2026-11-05"
    titles = [e["title"] for e in out["events"]]
    assert titles == ["Puente", "Cumpleaños de Ana", "Dentista"]  # all-day first
    dentist = out["events"][-1]
    assert dentist["start"] == "2026-11-05T17:00+01:00" and dentist["location"] == "C/ Mayor 1, Madrid"
    puente = out["events"][0]
    assert puente["all_day"] is True and puente["end"] == "2026-11-07"  # exclusive DTEND -> last day
    assert "end" not in out["events"][1]  # one-day all-day event
    report = next(r for r in fake.requests if r.method == "REPORT")
    body = report.content.decode()
    assert '<c:expand start="20261104T230000Z" end="20261105T230000Z"/>' in body
    assert '<c:time-range start="20261104T230000Z" end="20261105T230000Z"/>' in body


def test_agenda_calendar_filter_and_range(nc):
    out = run(nc.agenda("2026-11-05", 7, "cumpleaños"))
    assert [e["calendar"] for e in out["events"]] == ["Cumpleaños"] and out["to"] == "2026-11-11"
    with pytest.raises(ToolError):
        run(nc.agenda(None, 40, None))


# ------------------------------------------------------------------- tasks
def test_tasks_filters(nc):
    assert {t["title"] for t in run(nc.tasks(None, "open"))["tasks"]} == {
        "Llamar al fontanero", "Renovar DNI", "Comprar leche", "Comprar pan"}
    assert [t["title"] for t in run(nc.tasks(None, "due_today"))["tasks"]] == ["Renovar DNI"]
    assert run(nc.tasks(None, "due_today"))["tasks"][0]["priority"] == "high"
    assert [t["title"] for t in run(nc.tasks(None, "overdue"))["tasks"]] == ["Llamar al fontanero"]
    assert [t["title"] for t in run(nc.tasks(None, "upcoming_week"))["tasks"]] == ["Renovar DNI", "Comprar pan"]
    assert [t["title"] for t in run(nc.tasks("compra", "open"))["tasks"]] == ["Comprar pan", "Comprar leche"]


def test_add_task_creates_only(nc, fake):
    out = run(nc.add_task("Comprar huevos", "compra", "2026-11-06", "high", "Camperos"))
    assert out == {"status": "done", "task": "Comprar huevos", "list": "Compra", "due": "2026-11-06"}
    put = fake.puts()[-1]
    assert put.url.path.startswith(f"{CAL}compra/") and put.url.path.endswith(".ics")
    assert put.headers["If-None-Match"] == "*" and "If-Match" not in put.headers
    todo_ = Calendar.from_ical(put.content).walk("VTODO")[0]
    assert str(todo_["summary"]) == "Comprar huevos" and int(todo_["priority"]) == 1
    assert todo_.decoded("due").isoformat() == "2026-11-06" and str(todo_["description"]) == "Camperos"


def test_add_task_default_list_is_personal(nc, fake):
    run(nc.add_task("Pagar comunidad", None, None, None, None))
    assert fake.puts()[-1].url.path.startswith(f"{CAL}personal/")


def test_configured_defaults_win_over_personal(fake):
    c = cfg(default_tasklist="Compra", default_calendar="Personal")
    nc = ncm.Nextcloud(ncm.DavClient(c, transport=httpx.MockTransport(fake.handler)), c, now=lambda: NOW)
    run(nc.add_task("Comprar sal", None, None, None, None))
    assert fake.puts()[-1].url.path.startswith(f"{CAL}compra/")
    run(nc.add_task("Llamar al banco", "personal", None, None, None))  # an explicit list still wins
    assert fake.puts()[-1].url.path.startswith(f"{CAL}personal/")


def test_complete_task_replaces_exact_version(nc, fake):
    out = run(nc.complete_task("el fontanero", None))
    assert out == {"status": "done", "task": "Llamar al fontanero", "list": "Personal"}
    put = fake.puts()[-1]
    assert put.url.path == f"{CAL}personal/t1.ics" and put.headers["If-Match"] == '"e1"'
    t = Calendar.from_ical(put.content).walk("VTODO")[0]
    assert str(t["status"]) == "COMPLETED" and str(t["uid"]) == "t1" and int(t["percent-complete"]) == 100
    assert "Llamar al fontanero" not in {x["title"] for x in run(nc.tasks(None, "open"))["tasks"]}


def test_complete_task_ambiguous_and_conflict(nc, fake):
    with pytest.raises(ToolError, match="Ambiguous task"):
        run(nc.complete_task("comprar", None))
    real = fake.handler

    def changed_meanwhile(request):
        if request.method == "PUT":
            fake.objects[request.url.path] = ("changed", fake.objects[request.url.path][1])
        return real(request)

    fake.handler = changed_meanwhile
    c = cfg()
    nc2 = ncm.Nextcloud(ncm.DavClient(c, transport=httpx.MockTransport(lambda r: fake.handler(r))), c, now=lambda: NOW)
    with pytest.raises(ToolError, match="changed on another device"):
        run(nc2.complete_task("renovar dni", None))


# ------------------------------------------------------------------ events
def test_create_event_is_parked_until_confirmed(nc, fake):
    out = run(nc.create_event("Dentista Carmen", "2026-11-12T17:30", None, 45, False, "C/ Mayor 1, Madrid",
                              "Llevar informe", None, [30, 1440], "none", 1, None, None))
    assert out["status"] == "needs_confirmation" and not fake.puts()
    conf = out["confirmation"]
    assert conf["confirm_tool"] == "nc_confirm_action" and conf["action"] == "create_appointment"
    assert conf["details"] == {"title": "Dentista Carmen", "calendar": "Personal", "start": "2026-11-12T17:30+01:00",
                               "end": "2026-11-12T18:15+01:00", "all_day": False, "location": "C/ Mayor 1, Madrid",
                               "notes": "Llevar informe", "reminders_minutes_before": [30, 1440]}
    done = run(nc.parked.confirm(conf["confirmation_token"]))
    assert done["status"] == "done"
    put = fake.puts()[-1]
    assert put.headers["If-None-Match"] == "*" and put.url.path.startswith(f"{CAL}personal/")
    text = put.content.decode()
    assert "DTSTART;TZID=Europe/Madrid:20261112T173000" in text and "BEGIN:VTIMEZONE" in text
    assert "LOCATION:C/ Mayor 1\\, Madrid" in text and "ATTENDEE" not in text
    assert text.count("BEGIN:VALARM") == 2 and "TRIGGER:-PT30M" in text and "TRIGGER:-P1D" in text
    with pytest.raises(ToolError, match="Unknown or expired"):  # single use
        run(nc.parked.confirm(conf["confirmation_token"]))


def test_create_all_day_repeating_event(nc, fake):
    out = run(nc.create_event("Vacaciones", "2026-12-23", "2026-12-26", None, True, None, None, "personal", None,
                              "yearly", 1, 3, None))
    assert out["confirmation"]["details"]["end"] == "2026-12-26"
    run(nc.parked.confirm(out["confirmation"]["confirmation_token"]))
    ev = Calendar.from_ical(fake.puts()[-1].content).walk("VEVENT")[0]
    assert ev.decoded("dtstart").isoformat() == "2026-12-23" and ev.decoded("dtend").isoformat() == "2026-12-27"
    assert ev["rrule"]["FREQ"] == ["YEARLY"] and ev["rrule"]["COUNT"] == [3]


def test_create_event_validation(nc):
    with pytest.raises(ToolError, match="not after the start"):
        run(nc.create_event("X", "2026-11-12T17:30", "2026-11-12T17:00", None, False, None, None, None, None,
                            "none", 1, None, None))
    with pytest.raises(ToolError, match="start time"):
        run(nc.create_event("X", "2026-11-12", None, None, False, None, None, None, None, "none", 1, None, None))
    with pytest.raises(ToolError, match="Unknown calendar"):  # read-only birthdays calendar is not writable
        run(nc.create_event("X", "2026-11-12T10:00", None, None, False, None, None, "Cumpleaños", None,
                            "none", 1, None, None))


# ---------------------------------------------------------------- contacts
def test_find_contact_fuzzy_and_fields(nc):
    out = run(nc.find_contact("ana garcia"))
    assert out["count"] == 1
    ana = out["contacts"][0]
    assert ana["phones"] == [{"number": "+34 600 11 22 33", "type": "cell"}]
    assert ana["emails"] == [{"email": "ana@example.org", "type": "home"}] and ana["birthday"] == "--05-31"
    assert {c["name"] for c in run(nc.find_contact("ana"))["contacts"]} == {"Ana García"}  # word match beats 'Anabel'
    pedro = run(nc.find_contact("el fontanero"))["contacts"][0]
    assert pedro["phones"] == [{"number": "+34911223344", "type": "work"}]
    assert pedro["note"] == "Viene los sábados, mejor por la mañana"
    assert run(nc.find_contact("lopez"))["contacts"][0]["name"] == "Pedro Fontanero"  # by company
    assert run(nc.find_contact("sistema"))["count"] == 0  # system address book skipped


def test_contacts_cached_until_address_book_changes(nc, fake):
    def downloads():
        return [r for r in fake.requests if r.method == "REPORT" and r.url.path == f"{BOOK}contacts/"]

    run(nc.find_contact("ana"))
    run(nc.find_contact("pedro"))
    assert len(downloads()) == 1  # second lookup reused the cache (same ctag)
    body = downloads()[0].content.decode()
    assert '<card:prop name="TEL"/>' in body and "PHOTO" not in body  # partial retrieval, no photos
    out = run(nc.add_contact("Luis Nuevo", [{"number": "600 123 456"}], None, None, None, None, None))
    run(nc.parked.confirm(out["confirmation"]["confirmation_token"]))  # address book changes -> new ctag
    assert run(nc.find_contact("luis nuevo"))["count"] == 1
    # add_contact's duplicate check reused the cache; only the lookup after the write downloaded again.
    assert len(downloads()) == 2


def test_add_contact_parked_with_duplicate_warning(nc, fake):
    out = run(nc.add_contact("Ana García", [{"number": "+34 699 000 111", "type": "work"}], None, "05-31",
                             None, None, None))
    details = out["confirmation"]["details"]
    assert "already exists" in details["warning"] and details["address_book"] == "Contactos"
    assert not fake.puts()
    run(nc.parked.confirm(out["confirmation"]["confirmation_token"]))
    put = fake.puts()[-1]
    assert put.url.path.startswith(f"{BOOK}contacts/") and put.url.path.endswith(".vcf")
    assert put.headers["If-None-Match"] == "*" and put.headers["Content-Type"].startswith("text/vcard")
    back = vcard.parse(put.content.decode())
    assert back.name == "Ana García" and back.phones == [{"number": "+34 699 000 111", "type": "work"}]
    assert back.birthday == "--05-31"


def test_add_contact_validation(nc):
    with pytest.raises(ToolError, match="at least a phone"):
        run(nc.add_contact("Nadie", None, None, None, "ACME", None, None))
    with pytest.raises(ToolError, match="birthday"):
        run(nc.add_contact("Luis", [{"number": "1"}], None, "31 de mayo", None, None, None))


# ------------------------------------------------------------- the client
def test_client_refuses_anything_outside_the_rules(nc):
    client = nc.client
    for href in ("/remote.php/dav/calendars/otro/personal/", "/remote.php/dav/files/ana/x",
                 f"{CAL}../../files/ana/", "https://evil.example/remote.php/dav/calendars/ana/"):
        with pytest.raises(ToolError):
            run(client.request("PROPFIND", href, "<x/>"))
    with pytest.raises(ToolError, match="method not allowed"):
        run(client.request("DELETE", f"{CAL}personal/t1.ics"))
    with pytest.raises(ToolError, match="unconditional PUT"):
        run(client.request("PUT", f"{CAL}personal/x.ics", "x", {"Content-Type": "text/calendar"}))


def test_bad_credentials(fake):
    c = cfg()
    nc = ncm.Nextcloud(ncm.DavClient(c, transport=httpx.MockTransport(lambda r: httpx.Response(401))), c)
    with pytest.raises(ToolError, match="rejected"):
        run(nc.list_calendars())


def test_config(tmp_path):
    assert NextcloudConfig.from_env({"NEXTCLOUD_URL": "https://x"}) is None
    c = NextcloudConfig.from_env({"NEXTCLOUD_URL": "https://x/", "NEXTCLOUD_USER": "ana", "NEXTCLOUD_APP_PASSWORD": "p",
                                  "NEXTCLOUD_TIMEZONE": "Europe/Madrid", "NEXTCLOUD_CALENDARS": "Personal, Compra"})
    assert c.url == "https://x" and c.calendars == ("Personal", "Compra") and c.addressbook == "contacts"
    with pytest.raises(ValueError):
        NextcloudConfig.from_env({"NEXTCLOUD_URL": "https://x", "NEXTCLOUD_USER": "a/b", "NEXTCLOUD_APP_PASSWORD": "p"})
    with pytest.raises(ValueError, match="does not exist"):
        NextcloudConfig.from_env({"NEXTCLOUD_URL": "https://x", "NEXTCLOUD_USER": "a", "NEXTCLOUD_APP_PASSWORD": "p",
                                  "NEXTCLOUD_CERT_FILE": str(tmp_path / "none.pem")})


def test_registered_through_mcp(tmp_path, fake):
    env = {"FAMILIA_MCP_ENV_FILE": str(tmp_path / "none.env"), "NEXTCLOUD_URL": "https://nc.example",
           "NEXTCLOUD_USER": USER, "NEXTCLOUD_APP_PASSWORD": "secret", "NEXTCLOUD_TIMEZONE": "Europe/Madrid"}
    server = build_server(env, nextcloud_transport=httpx.MockTransport(fake.handler), nextcloud_now=lambda: NOW)

    async def go():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == {"nc_calendars", "nc_agenda", "nc_tasks", "nc_add_task", "nc_complete_task",
                                  "nc_create_event", "nc_find_contact", "nc_add_contact", "nc_confirm_action"}
            assert (tools["nc_confirm_action"].meta or {}).get("familia/internal") is True
            res = structured(await client.call_tool("nc_create_event", {"title": "Cena", "start": "2026-11-07T21:00"}))
            token = res["confirmation"]["confirmation_token"]
            return structured(await client.call_tool("nc_confirm_action", {"confirmation_token": token}))

    assert asyncio.run(go())["status"] == "done"
    assert fake.puts()[-1].url.path.startswith(f"{CAL}personal/")
