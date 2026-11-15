"""GPX / KML parsing and date inference for the locations module.

Files come from the user's own Nextcloud (PhoneTrack daily exports, trip
folders, Sports Tracker sessions, a list of chargers), so the formats vary:

- timed track points (PhoneTrack, Sports Tracker, Google history KML)
- untimed tracks and planned routes (<rtept>)
- lists of marked places (<wpt>, KML Point placemarks)

Dates of untimed content come from the file name, else from the folder name
(`2023_08_07-14_Galicia` = 7 to 14 Aug 2023; `2014_09_California` = Sep 2014;
`2022_07_02_Sevilla` = 2 Jul 2022). A file-name date is only trusted when it
fits the folder's dates, which resolves YY_MM_DD vs DD_MM_YY and ignores
export dates such as "GraphHopper-Track-2023-08-05" in a 2019 trip: the
file-name date must fall inside the folder's range or, failing that, in the
folder's year.
"""
from __future__ import annotations

import calendar
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

MONTHS = {
    # es
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    # en
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))

MIN_YEAR = 1990


@dataclass
class TrackPoint:
    lon: float
    lat: float
    ts: datetime | None = None   # UTC
    accuracy: float | None = None
    seg: int = 0
    kind: str = "track"          # track | route


@dataclass
class Waypoint:
    lon: float
    lat: float
    name: str = ""
    desc: str = ""
    type: str = ""
    ts: datetime | None = None


@dataclass
class Parsed:
    points: list[TrackPoint] = field(default_factory=list)
    waypoints: list[Waypoint] = field(default_factory=list)


DateRange = tuple[date, date]


# ---------------------------------------------------------------- parsing

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el: ET.Element, name: str) -> str:
    c = _child(el, name)
    return (c.text or "").strip() if c is not None else ""


def parse_time(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # GPX times are UTC by spec
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    if dt.year < MIN_YEAR:
        return None  # 1970 stamps (Sports Tracker without GPS time)
    if (dt.month, dt.day, dt.hour, dt.minute, dt.second) == (1, 1, 0, 0, 0):
        return None  # converters' placeholder (KML2GPX writes 20xx-01-01T00:00:00Z)
    return dt


def _coord(lon: str | None, lat: str | None) -> tuple[float, float] | None:
    try:
        x, y = float(lon), float(lat)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (-180 <= x <= 180 and -90 <= y <= 90) or (x == 0 and y == 0):
        return None
    return x, y


def _accuracy(pt: ET.Element) -> float | None:
    ext = _child(pt, "extensions")
    if ext is None:
        return None
    for el in ext.iter():
        if _local(el.tag) == "accuracy":
            try:
                return float(el.text or "")
            except ValueError:
                return None
    return None


def parse_gpx(data: bytes) -> Parsed:
    root = ET.fromstring(data)
    out = Parsed()
    seg_no = 0
    for el in root.iter():
        tag = _local(el.tag)
        if tag in ("trkseg", "rte"):
            seg_no += 1
            kind = "track" if tag == "trkseg" else "route"
            child = "trkpt" if tag == "trkseg" else "rtept"
            for pt in el:
                if _local(pt.tag) != child:
                    continue
                c = _coord(pt.get("lon"), pt.get("lat"))
                if c:
                    out.points.append(TrackPoint(c[0], c[1], parse_time(_text(pt, "time")),
                                                 _accuracy(pt), seg_no, kind))
        elif tag == "wpt":
            c = _coord(el.get("lon"), el.get("lat"))
            if c:
                out.waypoints.append(Waypoint(c[0], c[1], _text(el, "name"), _text(el, "desc"),
                                              _text(el, "type"), parse_time(_text(el, "time"))))
    return out


def _kml_coords(raw: str) -> list[tuple[float, float]]:
    out = []
    for tup in (raw or "").split():
        parts = tup.split(",")
        if len(parts) >= 2 and (c := _coord(parts[0], parts[1])):
            out.append(c)
    return out


def parse_kml(data: bytes) -> Parsed:
    root = ET.fromstring(data)
    out = Parsed()
    seg_no = 0
    for pm in root.iter():
        if _local(pm.tag) != "Placemark":
            continue
        name = _text(pm, "name")
        desc = _text(pm, "description") or _text(pm, "address")
        begin = end = None
        for el in pm.iter():
            t = _local(el.tag)
            if t == "TimeSpan":
                begin, end = parse_time(_text(el, "begin")), parse_time(_text(el, "end"))
            elif t == "TimeStamp":
                begin = end = parse_time(_text(el, "when"))
        for el in pm.iter():
            t = _local(el.tag)
            if t == "Point":
                cs = _kml_coords(_text(el, "coordinates"))
                if not cs:
                    continue
                if begin:  # a timed stay (Google location history): a point at arrival and departure
                    seg_no += 1
                    for ts in dict.fromkeys(x for x in (begin, end) if x):
                        out.points.append(TrackPoint(cs[0][0], cs[0][1], ts, None, seg_no))
                else:
                    out.waypoints.append(Waypoint(cs[0][0], cs[0][1], name, desc))
            elif t == "LineString":
                cs = _kml_coords(_text(el, "coordinates"))
                if not cs:
                    continue
                seg_no += 1
                times: list[datetime | None] = [None] * len(cs)
                if begin and end and len(cs) > 1:  # spread the span over the vertices
                    step = (end - begin) / (len(cs) - 1)
                    times = [begin + step * i for i in range(len(cs))]
                elif begin:
                    times = [begin] * len(cs)
                out.points.extend(TrackPoint(x, y, ts, None, seg_no) for (x, y), ts in zip(cs, times))
            elif t == "Track":  # gx:Track: <when> and <gx:coord> pairs
                whens = [parse_time(w.text or "") for w in el if _local(w.tag) == "when"]
                coords = [(w.text or "").split() for w in el if _local(w.tag) == "coord"]
                seg_no += 1
                for ts, c in zip(whens, coords):
                    if len(c) >= 2 and (xy := _coord(c[0], c[1])):
                        out.points.append(TrackPoint(xy[0], xy[1], ts, None, seg_no))
    return out


def parse_file(name: str, data: bytes) -> Parsed:
    if not data.strip():
        return Parsed()  # PhoneTrack writes empty files for days without fixes
    lower = name.lower()
    if lower.endswith(".gpx"):
        return parse_gpx(data)
    if lower.endswith(".kml"):
        return parse_kml(data)
    raise ValueError(f"unsupported file type: {name}")


# ---------------------------------------------------------------- dates

def _safe_date(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000
    try:
        dt = date(y, m, d)
    except ValueError:
        return None
    return dt if MIN_YEAR <= y <= date.today().year + 1 else None


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _range(start: date, end_day: int | None) -> DateRange:
    if end_day is None:
        return start, start
    if end_day >= start.day:
        try:
            return start, start.replace(day=end_day)
        except ValueError:
            return start, start
    nxt = _month_end(start.year, start.month) + timedelta(days=1)  # "28-3": into next month
    try:
        return start, nxt.replace(day=end_day)
    except ValueError:
        return start, start


_FOLDER_RE = re.compile(r"^(\d{4})_(\d{1,2})(?:_(\d{1,2})(?:-(\d{1,2}))?)?(?!\d)")


def folder_dates(folder: str) -> tuple[DateRange, bool] | None:
    """Dates in a trip folder name -> ((from, to), explicit_range).

    explicit_range is False for a single day (the trip's first day)."""
    m = _FOLDER_RE.match(folder.strip())
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    if not (1 <= mo <= 12) or not MIN_YEAR <= y <= date.today().year + 1:
        return None
    if m.group(3) is None:
        return (date(y, mo, 1), _month_end(y, mo)), True
    start = _safe_date(y, mo, int(m.group(3)))
    if start is None:
        return None
    end_day = int(m.group(4)) if m.group(4) else None
    return _range(start, end_day), end_day is not None


def filename_dates(name: str) -> list[DateRange]:
    """Every plausible date (or range) written in a file name, best first."""
    stem = re.sub(r"\.(gpx|kml)$", "", name, flags=re.I)
    low = stem.lower()
    out: list[DateRange] = []

    def add(r: DateRange | None) -> None:
        if r and r not in out:
            out.append(r)

    for m in re.finditer(r"(?<!\d)(\d{4})[-_.](\d{1,2})[-_.](\d{1,2})(?:-(\d{1,2})(?!\d))?(?![\d])", stem):
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            add(_range(d, int(m.group(4)) if m.group(4) else None))
    # "24-31Julio-2014", "30 julio 2014"
    for m in re.finditer(rf"(?<!\d)(\d{{1,2}})(?:-(\d{{1,2}}))?[\s_-]*({_MONTH_RE})[\s_,-]*(\d{{4}})", low):
        d = _safe_date(int(m.group(4)), MONTHS[m.group(3)], int(m.group(1)))
        if d:
            add(_range(d, int(m.group(2)) if m.group(2) else None))
    # "July 30, 2023"
    for m in re.finditer(rf"({_MONTH_RE})[\s_-]+(\d{{1,2}}),?[\s_-]+(\d{{4}})", low):
        add((d, d) if (d := _safe_date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))) else None)
    # Three short numbers: 22_07_02 (YY_MM_DD) or 2_7_22 / 01_06_2009 (D_M_Y)
    for m in re.finditer(r"(?<!\d)(\d{1,2})[-_. ](\d{1,2})[-_. ](\d{2}|\d{4})(?!\d)", stem):
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if len(m.group(3)) == 4:
            add((d, d) if (d := _safe_date(c, b, a)) else None)
            continue
        for d in (_safe_date(c, b, a), _safe_date(a, b, c)):  # D_M_YY, then YY_MM_DD
            add((d, d) if d else None)
    return out


def file_dates(filename: str, folder: tuple[DateRange, bool] | None) -> tuple[DateRange | None, str]:
    """Dates for a file's untimed content -> (range, source: file|folder|none)."""
    candidates = filename_dates(filename)
    if folder is None:
        return (candidates[0], "file") if candidates else (None, "none")
    (f_from, f_to), _explicit = folder
    for c_from, c_to in candidates:
        if f_from <= c_from and c_to <= f_to:
            return (c_from, c_to), "file"
    for c_from, c_to in candidates:
        if c_from.year in (f_from.year, f_to.year):
            return (c_from, c_to), "file"
    return (f_from, f_to), "folder"


_TIME_RE = re.compile(r"(?<!\d)(\d{1,2})[_:. ](\d{2})(?!\d)\s*(p\.?\s?m\.?|a\.?\s?m\.?)?", re.I)


def filename_time(name: str) -> str | None:
    """Clock time written after the date in a file name ("21_09_2008 20_48",
    "5_5_15 7_41 p. m.") -> "HH:MM", local to where it was recorded."""
    stem = re.sub(r"\.(gpx|kml)$", "", name, flags=re.I)
    parts = re.split(r"\s+", stem, maxsplit=1)
    if len(parts) < 2 or not filename_dates(parts[0]):
        return None
    m = _TIME_RE.match(parts[1].strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    ampm = (m.group(3) or "").lower().replace(".", "").replace(" ", "")
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    return f"{h:02d}:{mi:02d}" if h < 24 and mi < 60 else None
