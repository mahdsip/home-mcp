"""locations module: geometry, GPX/KML parsing, date rules, and the tools
against a fake Nextcloud (WebDAV files) and a fake Nominatim.

All coordinates and places are synthetic (rectangles near real cities).
"""
import asyncio
import hashlib
import os
import stat
from datetime import date
from urllib.parse import quote, unquote

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from conftest import structured
from familia_mcp import phonetrack as pt
from familia_mcp import tracks
from familia_mcp.config import LocationsConfig
from familia_mcp.geo import Shape
from familia_mcp.server import build_locations, build_server

USER = "ana"
HOME = f"/remote.php/dav/files/{USER}/"


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- geometry

SQUARE = {"type": "Polygon", "coordinates": [
    [[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]],
    [[0.4, 0.4], [0.6, 0.4], [0.6, 0.6], [0.4, 0.6], [0.4, 0.4]],  # hole
]}


def test_polygon_with_hole():
    s = Shape.from_geojson(SQUARE)
    assert s.contains((0.2, 0.2))
    assert not s.contains((0.5, 0.5))           # in the hole
    assert not s.contains((1.5, 0.5))
    assert s.distance_m((0.2, 0.2)) == 0
    assert 110_000 < s.distance_m((2, 0.5)) < 112_000   # ~1 degree of longitude at the equator


def test_line_and_segment_distance():
    street = Shape.from_geojson({"type": "LineString", "coordinates": [[-3.70, 40.42], [-3.69, 40.42]]})
    assert street.distance_m((-3.695, 40.4203)) == pytest.approx(33, abs=2)   # 0.0003 deg lat ~ 33 m
    # a segment crossing the street, with both ends 1 km away
    assert street.segment_distance_m((-3.695, 40.411), (-3.695, 40.429)) == 0
    area = Shape.from_geojson(SQUARE)
    assert area.segment_distance_m((-1, 0.2), (2, 0.2)) == 0                  # crosses the square


# ---------------------------------------------------------------- dates

def test_folder_dates():
    d = date
    assert tracks.folder_dates("2023_08_07-14_Galicia") == ((d(2023, 8, 7), d(2023, 8, 14)), True)
    assert tracks.folder_dates("2013_08_15-21Napoles y costa") == ((d(2013, 8, 15), d(2013, 8, 21)), True)
    assert tracks.folder_dates("2014_09_California") == ((d(2014, 9, 1), d(2014, 9, 30)), True)
    assert tracks.folder_dates("2022_07_02_PuntaUmbria-Sevilla") == ((d(2022, 7, 2), d(2022, 7, 2)), False)
    assert tracks.folder_dates("2022_07_28-3_Portugal") == ((d(2022, 7, 28), d(2022, 8, 3)), True)
    assert tracks.folder_dates("SportTracker") is None


def test_file_dates_rules():
    d = date
    single = tracks.folder_dates("2022_08_30_Motril")
    # YY_MM_DD vs DD_MM_YY resolved by the folder; another month of the same year is accepted
    assert tracks.file_dates("22_07_10BarcyElPalmar.gpx", single) == ((d(2022, 7, 10), d(2022, 7, 10)), "file")
    assert tracks.file_dates("IdaMadridBarcy2_7_22.gpx", single) == ((d(2022, 7, 2), d(2022, 7, 2)), "file")
    # export dates of another year are ignored
    assert tracks.file_dates("Route July 30, 2023 639 PM17.gpx", single)[1] == "folder"
    assert tracks.file_dates("GraphHopper-Track-2023-08-05-643km.gpx",
                             tracks.folder_dates("2019_07_29_Madrid-Cordoba"))[1] == "folder"
    # a range in the file name, Spanish month
    assert tracks.file_dates("Cerdeña-24-31Julio-2014.gpx", tracks.folder_dates("2014_07_24-31_cerdeña")) == (
        (d(2014, 7, 24), d(2014, 7, 31)), "file")
    # no folder dates (Sports Tracker): D_M_YY first
    assert tracks.file_dates("01_05_12 19_49.gpx", None) == ((d(2012, 5, 1), d(2012, 5, 1)), "file")
    assert tracks.file_dates("londres.gpx", None) == (None, "none")


def test_filename_time():
    assert tracks.filename_time("21_09_2008 20_48.gpx") == "20:48"
    assert tracks.filename_time("5_5_15 7_41 p. m..gpx") == "19:41"
    assert tracks.filename_time("15_08_2015 1_58 pm.gpx") == "13:58"
    assert tracks.filename_time("londres.gpx") is None


# ---------------------------------------------------------------- parsing

def gpx(body: str) -> bytes:
    return f'<?xml version="1.0"?><gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">{body}</gpx>'.encode()


def trk(*pts: tuple[float, float, str | None], acc: float | None = None) -> str:
    out = []
    for lat, lon, t in pts:
        ext = f"<extensions><accuracy>{acc}</accuracy></extensions>" if acc is not None else ""
        out.append(f'<trkpt lat="{lat}" lon="{lon}">' + (f"<time>{t}</time>" if t else "") + ext + "</trkpt>")
    return "<trk><trkseg>" + "".join(out) + "</trkseg></trk>"


def test_parse_gpx_kinds():
    data = gpx(trk((40.0, -3.0, "2026-11-10T08:00:00Z"), (40.1, -3.1, "1970-01-01T00:00:00Z"), acc=12.5)
               + '<rte><rtept lat="41" lon="-4"/></rte>'
               + '<wpt lat="42" lon="-5"><name>Castillo</name><type>x</type></wpt>'
               + '<trk><trkseg><trkpt lat="0" lon="0"/></trkseg></trk>')  # (0,0) is junk
    p = tracks.parse_gpx(data)
    assert [(x.kind, x.ts is not None, x.accuracy) for x in p.points] == [
        ("track", True, 12.5), ("track", False, 12.5), ("route", False, None)]
    assert p.waypoints[0].name == "Castillo"
    assert tracks.parse_file("empty.gpx", b"  ") .points == []


def test_parse_kml_google_history():
    data = b"""<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>
      <Placemark><name>Home</name><Point><coordinates>-3.70,40.40,0</coordinates></Point>
        <TimeSpan><begin>2021-12-04T17:00:00Z</begin><end>2021-12-05T08:00:00Z</end></TimeSpan></Placemark>
      <Placemark><name>Driving</name><LineString><coordinates>-3.70,40.40,0 -3.80,40.50,0 -3.90,40.60,0
        </coordinates></LineString><TimeSpan><begin>2021-12-05T08:00:00Z</begin><end>2021-12-05T09:00:00Z</end>
        </TimeSpan></Placemark>
      <Placemark><name>Castillo de Coca</name><Point><coordinates>-4.52,41.22</coordinates></Point></Placemark>
    </Document></kml>"""
    p = tracks.parse_kml(data)
    assert len(p.points) == 5 and all(x.ts for x in p.points)
    assert p.points[3].ts.isoformat() == "2021-12-05T08:30:00+00:00"   # spread over the vertices
    assert [w.name for w in p.waypoints] == ["Castillo de Coca"]


# ---------------------------------------------------------------- fakes

def rect(lon0, lat0, lon1, lat1):
    return {"type": "Polygon", "coordinates": [[[lon0, lat0], [lon1, lat0], [lon1, lat1], [lon0, lat1], [lon0, lat0]]]}


SEGOVIA = rect(-4.20, 40.90, -4.05, 41.00)
RETIRO = rect(-3.69, 40.410, -3.675, 40.420)
MERIDA = rect(-6.40, 38.88, -6.30, 38.96)
GRAN_VIA = [[-3.7100, 40.4200], [-3.7050, 40.4205]]
GRAN_VIA_2 = [[-3.7050, 40.4205], [-3.7000, 40.4195]]
SEGOVIA_COLOMBIA = rect(-74.75, 7.05, -74.65, 7.10)


class FakeNominatim:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["User-Agent"].startswith("home-mcp/")
        q = request.url.params
        if request.url.path == "/search":
            text = q["q"].lower()
            if "gran v" in text:
                base = {"category": "highway", "type": "primary", "name": "Gran Vía"}
                res = [dict(base, display_name="Gran Vía, Madrid", lon="-3.705", lat="40.42",
                            geojson={"type": "LineString", "coordinates": GRAN_VIA})]
                if q.get("dedupe") == "0":
                    res.append(dict(base, display_name="Gran Vía, Madrid", lon="-3.702", lat="40.42",
                                    geojson={"type": "LineString", "coordinates": GRAN_VIA_2}))
                return httpx.Response(200, json=res)
            if "segovia" in text:
                return httpx.Response(200, json=[
                    {"category": "boundary", "type": "administrative", "display_name": "Segovia, Colombia",
                     "lon": "-74.7", "lat": "7.07", "geojson": SEGOVIA_COLOMBIA},
                    {"category": "boundary", "type": "administrative", "display_name": "Segovia, España",
                     "lon": "-4.12", "lat": "40.95", "geojson": SEGOVIA}])
            if "retiro" in text:
                return httpx.Response(200, json=[{"category": "leisure", "type": "park", "display_name": "Retiro",
                                                  "lon": "-3.68", "lat": "40.415", "geojson": RETIRO}])
            if "mérida" in text or "merida" in text:
                return httpx.Response(200, json=[{"category": "boundary", "type": "administrative",
                                                  "display_name": "Mérida", "lon": "-6.35", "lat": "38.92",
                                                  "geojson": MERIDA}])
            if "los angeles" in text:
                return httpx.Response(200, json=[{"category": "place", "type": "city", "addresstype": "city",
                                                  "display_name": "Los Angeles", "lon": "-118.24", "lat": "34.05",
                                                  "geojson": {"type": "Point", "coordinates": [-118.24, 34.05]}}])
            return httpx.Response(200, json=[])
        if request.url.path == "/reverse":
            lon = float(q["lon"])
            city = "Segovia" if lon < -4 else "Madrid"
            return httpx.Response(200, json={"name": "x", "category": "highway",
                                             "address": {"road": "Calle Real", "city": city, "country_code": "es"}})
        return httpx.Response(404)


class FakeFiles:
    """Nextcloud WebDAV files: PROPFIND Depth 1 and GET; folder ETags change
    when anything inside changes, like Nextcloud's."""

    def __init__(self, tree: dict[str, bytes]):
        self.tree = dict(tree)
        self.requests: list[tuple[str, str]] = []
        self.sizes: dict[str, int] = {}       # override reported sizes
        self.fail_get: set[str] = set()      # paths answering 500

    def _etag(self, path: str) -> str:
        if path in self.tree:
            return hashlib.md5(self.tree[path]).hexdigest()
        kids = sorted(p for p in self.tree if p.startswith(path + "/"))
        return hashlib.md5("".join(k + self._etag(k) for k in kids).encode()).hexdigest()

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith(HOME)
        rel = unquote(request.url.path[len(HOME):]).rstrip("/")
        self.requests.append((request.method, rel))
        if request.method == "GET":
            if rel in self.fail_get:
                return httpx.Response(500)
            return httpx.Response(200, content=self.tree[rel]) if rel in self.tree else httpx.Response(404)
        assert request.method == "PROPFIND" and request.headers["Depth"] == "1"
        if not any(p.startswith(rel + "/") for p in self.tree):
            return httpx.Response(404)
        children = set()
        for p in self.tree:
            if p.startswith(rel + "/"):
                rest = p[len(rel) + 1:]
                children.add(rel + "/" + rest.split("/", 1)[0] + ("/" if "/" in rest else ""))
        resp = [self._response(rel + "/", True)] + [self._response(c, c.endswith("/")) for c in sorted(children)]
        body = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' + "".join(resp) + "</d:multistatus>"
        return httpx.Response(207, content=body.encode())

    def _response(self, rel: str, is_dir: bool) -> str:
        rt = "<d:collection/>" if is_dir else ""
        size = f"<d:getcontentlength>{self.sizes.get(rel, len(self.tree[rel]))}</d:getcontentlength>" if not is_dir else ""
        return (f"<d:response><d:href>{HOME}{quote(rel)}</d:href><d:propstat><d:prop>"
                f'<d:getetag>"{self._etag(rel.rstrip("/"))}"</d:getetag><d:resourcetype>{rt}</d:resourcetype>'
                f"{size}</d:prop></d:propstat></d:response>")


def day_file(*pts):
    return gpx(trk(*pts, acc=20))


TREE = {
    # 2026-11-08: in Segovia (UTC 10:00 = 11:00 Madrid)
    "Maps/daily/Personal_daily_2026-11-08_Pixel.gpx": day_file((40.95, -4.12, "2026-11-08T10:00:00Z"),
                                                                (40.95, -4.12, "2026-11-08T11:00:00Z")),
    # 2026-11-10: Madrid; two fixes either side of Gran Vía, 20 min apart
    "Maps/daily/Personal_daily_2026-11-10_Pixel.gpx": day_file((40.4150, -3.7060, "2026-11-10T09:00:00Z"),
                                                                (40.4260, -3.7060, "2026-11-10T09:20:00Z")),
    # 2026-11-11: Madrid, far from everything; 2026-11-09 has no file at all
    "Maps/daily/Personal_daily_2026-11-11_Pixel.gpx": day_file((40.30, -3.60, "2026-11-11T09:00:00Z")),
    "Maps/daily/empty_2026-11-12.gpx": b"",
    # California: 05:00 UTC on the 10th is 22:00 on the 9th in Los Angeles
    # converter placeholder time: must not count as GPS
    "Maps/2014_09_California/california.gpx": gpx(trk((33.98, -118.47, "2010-01-01T00:00:00Z"))),
    "Maps/2014_09_California/la.gpx": gpx(trk((34.05, -118.24, "2014-09-10T05:00:00Z"))
                                          + '<wpt lat="34.01" lon="-118.49"><name>Venice</name></wpt>'),
    # A planned route through Segovia, dated by the folder range
    "Maps/2023_08_07-14_Galicia/ruta.gpx": gpx('<rte><rtept lat="40.95" lon="-4.30"/>'
                                               '<rtept lat="40.95" lon="-3.90"/></rte>'),
    "Maps/2023_08_07-14_Galicia/notes.txt": b"ignored",
    "Maps/SportTracker/02_05_2014 12_53.gpx": gpx(trk((40.415, -3.68, "2014-05-02T10:53:00Z"),
                                                       (40.416, -3.681, "2014-05-02T11:30:00Z"))),
    "Maps/SportTracker/21_09_2008 20_48.gpx": gpx(trk((40.418, -3.684, None), (40.419, -3.685, None))),
    "Maps/SportTracker/lejos.gpx": gpx(trk((41.5, -2.0, None), (41.6, -2.1, None))),
    "Maps/Cargadores/cargadores.gpx": gpx(
        '<wpt lat="38.92" lon="-6.34"><name>IONITY Mérida</name>'
        "<desc>Potencia máxima verificada: 350 kW.</desc><type>IONITY | Ultra</type></wpt>"
        '<wpt lat="38.70" lon="-6.40"><name>Lejano</name><desc>50 kW</desc></wpt>'
        '<wpt lat="39.10" lon="-6.35"><name>Norte</name><desc>Potencia máxima verificada: 60 kW</desc></wpt>'),
}


def make(tmp_path, tree=None):
    files = FakeFiles(tree or TREE)
    nom = FakeNominatim()
    env = {"NEXTCLOUD_URL": "https://nc.example", "NEXTCLOUD_USER": USER, "NEXTCLOUD_APP_PASSWORD": "p",
           "PHONETRACK_ROOT": "Maps", "PHONETRACK_DB": str(tmp_path / "db" / "loc.sqlite")}
    cfg = LocationsConfig.from_env(env)
    loc = build_locations(cfg, httpx.MockTransport(files.handler), httpx.MockTransport(nom.handler),
                          nominatim_interval_s=0)
    return loc, files, nom


@pytest.fixture
def synced(tmp_path):
    loc, files, nom = make(tmp_path)
    stats = run(loc.sync())
    assert stats.complete and not stats.errors
    return loc, files, nom


# ---------------------------------------------------------------- sync

def test_sync_incremental(tmp_path):
    loc, files, _ = make(tmp_path)
    first = run(loc.sync())
    assert first.files_indexed == 11  # notes.txt skipped
    files.requests.clear()
    assert run(loc.sync()).files_indexed == 0
    assert files.requests == [("PROPFIND", "Maps")]          # unchanged tree: one request
    files.tree["Maps/daily/Personal_daily_2026-11-11_Pixel.gpx"] = day_file((40.31, -3.61, "2026-11-11T10:00:00Z"))
    del files.tree["Maps/SportTracker/lejos.gpx"]
    files.requests.clear()
    s = run(loc.sync())
    assert (s.files_indexed, s.files_removed) == (1, 1)
    assert [r for r in files.requests if r[0] == "GET"] == [("GET", "Maps/daily/Personal_daily_2026-11-11_Pixel.gpx")]
    assert sorted({r[1] for r in files.requests if r[0] == "PROPFIND"}) == ["Maps", "Maps/SportTracker", "Maps/daily"]


def test_sync_retries_failed_files_and_skips_huge_ones(tmp_path):
    loc, files, _ = make(tmp_path)
    flaky = "Maps/daily/Personal_daily_2026-11-08_Pixel.gpx"
    files.fail_get.add(flaky)
    files.sizes["Maps/SportTracker/lejos.gpx"] = 99 * 1024 * 1024
    s = run(loc.sync())
    assert s.errors and not loc.index.meta("root_etag")
    files.fail_get.clear()
    files.requests.clear()
    s = run(loc.sync())
    assert not s.errors and s.files_indexed == 1
    assert ("GET", flaky) in files.requests and ("GET", "Maps/SportTracker/lejos.gpx") not in files.requests
    row = loc.index.db.execute("SELECT error FROM files WHERE name='lejos.gpx'").fetchone()
    assert row["error"].startswith("too large")


def test_index_is_private(synced):
    loc, _, _ = synced
    assert stat.S_IMODE(os.stat(loc.cfg.db_path).st_mode) == 0o600


def test_sync_budget_resumes(tmp_path):
    loc, _, _ = make(tmp_path)
    s = run(loc.sync(budget_s=0))
    assert not s.complete
    assert run(loc.sync()).complete
    assert loc.index.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 11


def test_empty_index_asks_for_initial_sync(tmp_path):
    loc, _, _ = make(tmp_path)
    with pytest.raises(ToolError, match="--sync-locations"):
        run(loc.was_i_at("Segovia", "2026-11-08", None, None))


def test_path_guard(tmp_path):
    loc, _, _ = make(tmp_path)
    with pytest.raises(ToolError, match="not allowed"):
        run(loc.files.get("Maps/../Documents/secret.txt"))
    with pytest.raises(ToolError, match="not allowed"):
        run(loc.files.get("Documents/secret.txt"))


# ---------------------------------------------------------------- tools

def test_was_i_at_city_with_local_time(synced):
    loc, _, nom = synced
    r = run(loc.was_i_at("Segovia", "2026-11-08", None, None))
    assert r["found"] and r["place"] == "Segovia, España"     # skipped the Colombian one: no fix there
    v = r["visits"][0]
    assert (v["date"], v["first_time"], v["last_time"], v["evidence"]) == ("2026-11-08", "11:00", "12:00", "gps")
    assert v["time_zone"] == "Europe/Madrid"
    run(loc.was_i_at("Segovia", "2026-11-08", None, None))
    assert sum(1 for q in nom.requests if q.url.path == "/search") == 1   # cached


def test_was_i_at_street_between_fixes(synced):
    loc, _, _ = synced
    r = run(loc.was_i_at("Gran Vía, Madrid", "2026-11-10", None, None))
    assert r["found"] and r["visits"][0]["evidence"] == "gps_path"
    assert (r["visits"][0]["first_time"], r["visits"][0]["last_time"]) == ("10:00", "10:20")


def test_was_i_at_no_data_vs_not_there(synced):
    loc, _, _ = synced
    r = run(loc.was_i_at("Segovia", None, "2026-11-09", "2026-11-12"))
    assert not r["found"]
    assert r["days_without_gps_data"] == ["2026-11-09", "2026-11-12"]   # 10 and 11 have data elsewhere
    r = run(loc.was_i_at("Segovia", None, "2026-11-01", "2026-11-07"))
    assert r["days_without_gps_data"] == ["2026-11-01 to 2026-11-07"]


def test_local_day_comes_from_coordinates(synced):
    loc, _, _ = synced
    r = run(loc.was_i_at("Los Angeles", "2014-09-09", None, None))
    assert r["found"] and r["visits"][0]["first_time"] == "22:00"
    assert r["visits"][0]["time_zone"] == "America/Los_Angeles"
    assert r["visits"][0]["sources"] == ["2014_09_California"]
    assert not run(loc.was_i_at("Los Angeles", "2014-09-10", None, None))["found"]


def test_planned_route_and_range_dates(synced):
    loc, _, _ = synced
    r = run(loc.was_i_at("Segovia", "2023-08-10", None, None))      # inside 7-14
    assert not r["found"]
    ev = r["other_evidence"][0]
    assert ev["evidence"] == "planned_route" and ev["dates"] == "2023-08-07 to 2023-08-14"
    assert "other_evidence" not in run(loc.was_i_at("Segovia", "2023-08-20", None, None))


def test_where_was_i(synced):
    loc, _, _ = synced
    r = run(loc.where_was_i("2026-11-08"))
    assert r["gps_data"] and r["stops"][0]["from"] == "11:00" and r["stops"][0]["to"] == "12:00"
    assert r["localities"][0]["place"] == "Segovia"
    r = run(loc.where_was_i("2014-09-09"))
    assert r["trips_that_day"] == ["2014_09_California"] and r["time_zones"] == ["America/Los_Angeles"]
    assert run(loc.where_was_i("2026-11-09"))["gps_data"] is False


def test_trips(synced):
    loc, _, _ = synced
    r = run(loc.list_trips(2014, None))
    assert [t["trip"] for t in r["trips"]] == ["2014_09_California"]
    assert r["trips"][0]["content"] == ["gps", "routes/tracks", "marked places"]
    assert run(loc.list_trips(None, "venice"))["count"] == 1        # matches a marked place
    s = run(loc.trip_summary("galicia"))
    assert s["trip"] == "2023_08_07-14_Galicia" and s["dates_are_range"]
    assert run(loc.trip_summary("California"))["gps_days"] == "2014-09-09"   # not the 2010 placeholder
    assert s["files_detail"] == [{"file": "ruta.gpx", "dates": "2023-08-07 to 2023-08-14", "dates_from": "folder",
                                  "content": "route/track"}]
    assert "Segovia" in s["localities"]


def test_runs_near(synced):
    loc, _, _ = synced
    r = run(loc.runs_near("Retiro", 300, None, None))
    assert r["count"] == 2 and (r["first"], r["last"]) == ("2008-09-21", "2014-05-02")
    old, new = r["runs"]
    assert old["start_time"] == "20:48" and "file name" in old["note"]
    assert new["start_time"] == "12:53" and new["duration_min"] == 37
    assert run(loc.runs_near("Retiro", 300, "2010-01-01", None))["count"] == 1


def test_chargers_near(synced):
    loc, _, _ = synced
    # radius counts from the place's boundary: Norte is ~15 km and Lejano ~20 km outside it
    r = run(loc.chargers_near("Mérida", 25, None))
    assert [c["name"] for c in r["chargers"]] == ["IONITY Mérida", "Norte", "Lejano"]
    assert [c["name"] for c in run(loc.chargers_near("Mérida", 17, None))["chargers"]] == ["IONITY Mérida", "Norte"]
    assert r["chargers"][0]["max_kw"] == 350 and r["chargers"][0]["inside_place"]
    assert [c["name"] for c in run(loc.chargers_near("Mérida", 25, 100))["chargers"]] == ["IONITY Mérida"]


def test_bad_inputs(synced):
    loc, _, _ = synced
    with pytest.raises(ToolError, match="date like"):
        run(loc.was_i_at("Segovia", "8 de noviembre", None, None))
    with pytest.raises(ToolError, match="does not know"):
        run(loc.was_i_at("Nowhere-land", "2026-11-08", None, None))


def test_registered_through_mcp(tmp_path):
    files, nom = FakeFiles(TREE), FakeNominatim()
    env = {"FAMILIA_MCP_ENV_FILE": str(tmp_path / "none.env"), "NEXTCLOUD_URL": "https://nc.example",
           "NEXTCLOUD_USER": USER, "NEXTCLOUD_APP_PASSWORD": "p", "PHONETRACK_ROOT": "Maps",
           "PHONETRACK_DB": str(tmp_path / "loc.sqlite")}
    server = build_server(env, nextcloud_transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                          locations_transport=httpx.MockTransport(files.handler),
                          nominatim_transport=httpx.MockTransport(nom.handler))

    async def go():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert {"loc_was_i_at", "loc_where_was_i", "loc_list_trips", "loc_trip_summary",
                    "loc_runs_near", "loc_chargers_near"} <= set(tools)
            assert all(tools[n].annotations.read_only_hint for n in tools if n.startswith("loc_"))
            res = await client.call_tool("loc_was_i_at", {"place": "Segovia", "date": "2026-11-08"})
            return res

    res = run(go())
    assert res.is_error  # index not built yet
    cfg = LocationsConfig.from_env(env)
    run(build_locations(cfg, httpx.MockTransport(files.handler), httpx.MockTransport(nom.handler)).sync())

    async def again():
        async with Client(server) as client:
            return structured(await client.call_tool("loc_was_i_at", {"place": "Segovia", "date": "2026-11-08"}))

    assert run(again())["found"] is True


def test_config():
    base = {"NEXTCLOUD_URL": "https://x", "NEXTCLOUD_USER": "ana", "NEXTCLOUD_APP_PASSWORD": "p"}
    assert LocationsConfig.from_env(base) is None
    assert LocationsConfig.from_env({"PHONETRACK_ROOT": "Maps"}) is None
    c = LocationsConfig.from_env(dict(base, PHONETRACK_ROOT="/Maps/"))
    assert c.root == "Maps" and c.daily_folder == "daily" and c.nominatim_url.startswith("https://nominatim")
    with pytest.raises(ValueError):
        LocationsConfig.from_env(dict(base, PHONETRACK_ROOT="Maps/../x"))
    with pytest.raises(ValueError):
        LocationsConfig.from_env(dict(base, PHONETRACK_ROOT="Maps", PHONETRACK_SPORT_FOLDER="a/b"))


# ---------------------------------------------------------------- time budgets and robustness

def test_index_dir_is_private(tmp_path):
    loc, _, _ = make(tmp_path)
    assert stat.S_IMODE(os.stat(os.path.dirname(loc.cfg.db_path)).st_mode) == 0o700
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    os.chmod(shared, 0o755)
    with pytest.raises(ValueError, match="chmod 700"):
        pt.Index(str(shared / "loc.sqlite"))


def test_stale_index_refreshes_in_background(tmp_path):
    loc, files, _ = make(tmp_path)
    run(loc.sync())
    files.tree["Maps/daily/Personal_daily_2026-11-14_Pixel.gpx"] = day_file((40.95, -4.12, "2026-11-14T10:00:00Z"))
    loc.index.set_meta("last_sync", "0")
    object.__setattr__(loc.cfg, "sync_budget_s", 0)     # no time to sync inline

    async def go():
        first = await loc.was_i_at("Segovia", "2026-11-14", None, None)
        await loc._background                           # the call returned; the sync goes on
        second = await loc.was_i_at("Segovia", "2026-11-14", None, None)
        return first, second

    first, second = run(go())
    assert not first["found"] and "in progress" in first["index_note"]
    assert second["found"] and "index_note" not in second


def test_summaries_stop_geocoding_at_the_deadline(synced):
    loc, _, nom = synced
    object.__setattr__(loc.cfg, "tool_budget_s", 0)     # no room for any Nominatim lookup
    r = run(loc.where_was_i("2026-11-08"))
    assert r["gps_data"] and r["stops"][0]["place"] is None and r.get("localities_truncated")
    assert not any(q.url.path == "/reverse" for q in nom.requests)


def test_broken_index_does_not_take_the_server_down(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)                                 # cannot create the database
    env = {"FAMILIA_MCP_ENV_FILE": str(tmp_path / "none.env"), "NEXTCLOUD_URL": "https://nc.example",
           "NEXTCLOUD_USER": USER, "NEXTCLOUD_APP_PASSWORD": "p", "PHONETRACK_ROOT": "Maps",
           "PHONETRACK_DB": str(ro / "db" / "loc.sqlite")}
    server = build_server(env, nextcloud_transport=httpx.MockTransport(lambda r: httpx.Response(404)))

    async def names():
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    tools = run(names())
    os.chmod(ro, 0o700)
    assert "nc_agenda" in tools and not any(n.startswith("loc_") for n in tools)


# ---------------------------------------------------------------- photo evidence (fake PhotoPrism MCP)

import json as _json  # noqa: E402
import math as _math  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402

from familia_mcp import photos as ph  # noqa: E402
from familia_mcp.locphotos import PhotoEvidence  # noqa: E402

PP_PEOPLE = [{"uid": "jc", "name": "Carmen", "media_count": 2}, {"uid": "ji", "name": "Inés", "media_count": 1}]


def photo(uid, lat, lng, local, *, people=(), title=None, place="Segovia, Castilla y León, Spain", located=True):
    return {"uid": uid, "type": "image", "title": title or uid, "taken_at_local": local, "taken_at": local + "Z",
            "place": {"country": "es", "name": place}, "camera": "Google Pixel 11 Pro",
            "people": [{"uid": p, "name": next(x["name"] for x in PP_PEOPLE if x["uid"] == p)} for p in people],
            "_lat": lat, "_lng": lng, "_located": located}


PP_LIBRARY = [
    # 2026-11-08 in Segovia, same day as the GPS fixes; Carmen appears in one
    photo("s1", 40.951, -4.121, "2026-11-08T11:30:00", people=["jc"], title="Acueducto"),
    photo("s2", 40.949, -4.119, "2026-11-08T12:10:00", title="Catedral"),
    # 2025-03-01: photos but no GPS file at all (Inés)
    photo("s3", 40.950, -4.120, "2025-03-01T17:05:00", people=["ji"], title="Alcázar"),
    # inside the search circle, outside Segovia's polygon (rect ends at lon -4.20): must not count
    photo("x1", 40.950, -4.215, "2026-11-20T10:00:00", place="La Losa, Castilla y León, Spain"),
    # location stripped from metadata: cannot be confirmed
    photo("x2", 40.950, -4.120, "2026-11-21T10:00:00", located=False),
    # Madrid, 2026-11-09: a day without GPS where the user took pictures
    photo("m1", 40.4168, -3.7038, "2025-03-02T09:00:00", place="Madrid, Comunidad de Madrid, Spain"),
    # Los Angeles: 22:00 local on 2014-09-09 (05:00 UTC on the 10th)
    photo("la1", 34.05, -118.24, "2014-09-09T22:00:00", place="Los Angeles, California, United States"),
]


def _km(lat1, lng1, lat2, lng2):
    p = _math.pi / 180
    a = (_math.sin((lat2 - lat1) * p / 2) ** 2
         + _math.cos(lat1 * p) * _math.cos(lat2 * p) * _math.sin((lng2 - lng1) * p / 2) ** 2)
    return 12742 * _math.asin(_math.sqrt(a))


def fake_photoprism(calls):
    srv = MCPServer(name="photoprism")

    @srv.tool(name="list_people")
    def list_people(query: str = "", limit: int = 20, offset: int = 0) -> dict:
        calls.append(("list_people", {}))
        items = PP_PEOPLE[offset: offset + limit]
        return {"items": items, "total": len(PP_PEOPLE), "limit": limit, "offset": offset,
                "truncated": offset + limit < len(PP_PEOPLE)}

    @srv.tool(name="search_media")
    def search_media(query: str = "", person: str = "", taken_after: str = "", taken_before: str = "",
                     order: str = "newest", limit: int = 20, offset: int = 0, detail: str = "brief") -> dict:
        calls.append(("search_media", dict(query=query, person=person, taken_after=taken_after,
                                           taken_before=taken_before, offset=offset)))
        f = dict(t.split(":", 1) for t in query.split() if ":" in t)
        found = []
        for m in sorted(PP_LIBRARY, key=lambda m: m["taken_at_local"], reverse=order != "oldest"):
            d = m["taken_at_local"][:10]
            if person and person not in [p["uid"] for p in m["people"]]:
                continue
            if taken_after and d < taken_after or taken_before and d >= taken_before:
                continue
            if "dist" in f and _km(float(f["lat"]), float(f["lng"]), m["_lat"], m["_lng"]) > float(f["dist"]):
                continue
            item = {k: v for k, v in m.items() if not k.startswith("_")}
            if detail != "full":
                item.pop("people"); item.pop("camera")
            found.append(item)
        chunk = found[offset: offset + limit]
        out = {"items": chunk, "limit": limit, "offset": offset}
        if offset + limit < len(found):
            out["next_offset"] = offset + limit
        return out

    @srv.tool(name="get_media")
    def get_media(uid: str) -> dict:
        calls.append(("get_media", uid))
        m = next(x for x in PP_LIBRARY if x["uid"] == uid)
        item = {k: v for k, v in m.items() if not k.startswith("_")}
        if m["_located"]:
            item["location"] = {"lat": m["_lat"], "lng": m["_lng"], "altitude": 1000}
        return item

    return srv


@pytest.fixture
def pp_calls():
    return []


@pytest.fixture
def withphotos(synced, pp_calls):
    loc, files, nom = synced
    loc.photos = PhotoEvidence(ph.server_connect(fake_photoprism(pp_calls)))
    return loc


def _no_coordinates(obj):
    text = _json.dumps(obj)
    return '"lat"' not in text and '"lng"' not in text and "40.95" not in text


def test_photo_day_alongside_gps(withphotos, pp_calls):
    r = run(withphotos.was_i_at("Segovia", "2026-11-08", None, None))
    assert r["found"] and r["visits"][0]["evidence"] == "gps"
    day = r["photo_days"][0]
    assert (day["date"], day["evidence"], day["first_time"], day["last_time"]) == ("2026-11-08", "photo", "11:30", "12:10")
    assert day["gps_that_day"] is True and day["people"] == ["Carmen"] and day["photos_nearby"] == 2
    assert day["place"] == "Segovia, Castilla y León" and day["examples"] == ["Acueducto", "Catedral"]
    q = [c[1] for c in pp_calls if c[0] == "search_media"][0]
    assert q["query"].startswith("lat:") and "dist:" in q["query"]
    assert (q["taken_after"], q["taken_before"]) == ("2026-11-07", "2026-11-10")   # widened, then filtered
    assert _no_coordinates(r)


def test_photos_only_day_counts_as_found(withphotos):
    r = run(withphotos.was_i_at("Segovia", "2025-03-01", None, None))
    assert r["found"] and r["visits"] == []
    assert r["photo_days"][0]["gps_that_day"] is False and r["days_without_gps_data"] == ["2025-03-01"]


def test_photo_outside_the_place_or_without_position_is_ignored(withphotos, pp_calls):
    r = run(withphotos.was_i_at("Segovia", None, "2026-11-20", "2026-11-21"))
    assert not r["found"] and "photo_days" not in r
    assert ("get_media", "x1") in pp_calls and ("get_media", "x2") in pp_calls   # both were checked


def test_all_time_lists_every_photo_day(withphotos):
    r = run(withphotos.was_i_at("Segovia", None, None, None))
    assert [d["date"] for d in r["photo_days"]] == ["2025-03-01", "2026-11-08"]


def test_person_uses_photos_only(withphotos):
    r = run(withphotos.was_i_at("Segovia", None, None, None, person="ines"))
    assert r["person"] == "Inés" and r["visits"] == []                  # GPS is the phone owner's
    assert [d["date"] for d in r["photo_days"]] == ["2025-03-01"]
    r = run(withphotos.was_i_at("Segovia", "2026-11-08", None, None, person="Carmen"))
    assert r["found"] and r["photo_days"][0]["people"] == ["Carmen"]
    with pytest.raises(ToolError, match="Unknown person"):
        run(withphotos.was_i_at("Segovia", None, None, None, person="Pepe"))


def test_person_needs_photo_library(synced):
    loc, _, _ = synced
    with pytest.raises(ToolError, match="photo library"):
        run(loc.was_i_at("Segovia", None, None, None, person="Carmen"))


def test_lookups_are_capped(withphotos, pp_calls, monkeypatch):
    import familia_mcp.locphotos as lp
    monkeypatch.setattr(lp, "MAX_LOOKUPS", 1)
    r = run(withphotos.was_i_at("Segovia", None, None, None))
    assert sum(1 for c in pp_calls if c[0] == "get_media") == 1
    assert "photos_note" in r


def test_where_was_i_with_photos_and_no_gps(withphotos):
    r = run(withphotos.where_was_i("2025-03-01"))
    assert r["gps_data"] is False and "photos" in r["hint"]
    p = r["photos"]
    assert (p["count"], p["first_time"], p["last_time"], p["people"]) == (1, "17:05", "17:05", ["Inés"])
    assert p["places"] == ["Segovia, Castilla y León"] and _no_coordinates(r)
    r = run(withphotos.where_was_i("2026-11-08"))
    assert r["gps_data"] and r["photos"]["count"] == 2


def test_trip_summary_photos_use_local_day(withphotos):
    s = run(withphotos.trip_summary("California"))
    assert s["photos"]["by_day"] == {"2014-09-09": 1}
    assert s["photos"]["places"] == ["Los Angeles, California"]
    assert "first_time" not in s["photos"]                                 # meaningless across days


def test_photoprism_down_keeps_gps_answer(synced):
    loc, _, _ = synced

    def broken():
        raise ToolError("PhotoPrism is not reachable (ConnectError).")

    class Down:
        def __call__(self):
            return self

        async def __aenter__(self):
            broken()

        async def __aexit__(self, *a):
            return False

    loc.photos = PhotoEvidence(Down())
    r = run(loc.was_i_at("Segovia", "2026-11-08", None, None))
    assert r["found"] and r["visits"] and "could not be checked" in r["photos_note"]
    r = run(loc.where_was_i("2026-11-08"))
    assert r["gps_data"] and "photos_note" in r


def test_person_param_through_mcp(tmp_path, pp_calls):
    files, nom = FakeFiles(TREE), FakeNominatim()
    env = {"FAMILIA_MCP_ENV_FILE": str(tmp_path / "none.env"), "NEXTCLOUD_URL": "https://nc.example",
           "NEXTCLOUD_USER": USER, "NEXTCLOUD_APP_PASSWORD": "p", "PHONETRACK_ROOT": "Maps",
           "PHONETRACK_DB": str(tmp_path / "loc.sqlite"), "PHOTOPRISM_URL": "http://pp", "PHOTOPRISM_TOKEN": "t"}
    cfg = LocationsConfig.from_env(env)
    run(build_locations(cfg, httpx.MockTransport(files.handler), httpx.MockTransport(nom.handler)).sync())
    server = build_server(env, nextcloud_transport=httpx.MockTransport(lambda r: httpx.Response(404)),
                          locations_transport=httpx.MockTransport(files.handler),
                          nominatim_transport=httpx.MockTransport(nom.handler),
                          photos_target=fake_photoprism(pp_calls))

    async def go():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert "person" in tools["loc_was_i_at"].input_schema["properties"]
            return structured(await client.call_tool("loc_was_i_at", {"place": "Segovia", "person": "Inés"}))

    r = run(go())
    assert r["person"] == "Inés" and [d["date"] for d in r["photo_days"]] == ["2025-03-01"]
