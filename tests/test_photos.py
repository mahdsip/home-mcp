"""photos module against a fake PhotoPrism MCP server (in-process, same tool
names and result shapes as PhotoPrism's /api/v1/mcp)."""

import asyncio
from datetime import date

import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from conftest import structured
from familia_mcp import photos as ph
from familia_mcp.server import build_server

PEOPLE = [{"uid": "j1", "name": "Carmen", "media_count": 300}, {"uid": "j2", "name": "Inés", "media_count": 40},
          {"uid": "j3", "name": "Inesa", "media_count": 5}]
ALBUMS = [{"uid": "a1", "title": "Asturias Octubre", "media_count": 3, "created_at": "2024-10-13T06:49:54Z"},
          {"uid": "a2", "title": "Playa Semana Santa", "media_count": 2, "created_at": "2026-04-20T05:39:04Z"},
          {"uid": "a3", "title": "Pueblo Semana Santa", "media_count": 1, "created_at": "2024-04-01T00:00:00Z"}]


def media(n, *, person=None, album=None, year=2019, month=7, day=1, place="Llanes, Asturias, Spain", kind="image"):
    return {"uid": f"p{n}", "type": kind, "title": f"T{n}", "favorite": n == 0,
            "taken_at_local": f"{year}-{month:02d}-{day:02d}T12:00:{n % 60:02d}", "taken_at": "ignored",
            "place": {"country": "es", "name": place}, "_person": person, "_album": album}


LIBRARY = (
    [media(i, person="j1", year=2019) for i in range(120)]                      # 120 of Carmen in 2019
    + [media(200 + i, person="j1", year=2020, place="Madrid, Comunidad de Madrid, Spain") for i in range(10)]
    + [media(300 + i, album="a1", year=2013, kind="video" if i == 0 else "image") for i in range(3)]
    + [media(400, year=2006, month=11, day=2, place="Paris, Île-de-France, France"),
       media(401, year=2016, month=11, day=2, place="Pampaneira, Andalucía, Spain"),
       media(402, year=2016, month=11, day=3)]                                   # different day
)


def fake_photoprism(calls):
    srv = MCPServer(name="photoprism")

    def page(items, limit, offset, total=True):
        chunk = items[offset: offset + limit]
        out = {"items": chunk, "limit": limit, "offset": offset, "truncated": offset + limit < len(items)}
        if total:
            out["total"] = len(items)
        return out

    @srv.tool(name="list_people")
    def list_people(query: str = "", limit: int = 20, offset: int = 0) -> dict:
        calls.append(("list_people", query))
        return page([p for p in PEOPLE if query.lower() in p["name"].lower()], limit, offset)

    @srv.tool(name="list_collections")
    def list_collections(type: str = "album", query: str = "", limit: int = 20, offset: int = 0) -> dict:
        calls.append(("list_collections", type))
        return page([a for a in ALBUMS if query.lower() in a["title"].lower()], limit, offset)

    @srv.tool(name="search_media")
    def search_media(query: str = "", type: str = "", collection: str = "", person: str = "",
                     taken_after: str = "", taken_before: str = "", order: str = "newest",
                     limit: int = 20, offset: int = 0) -> dict:
        calls.append(("search_media", dict(query=query, person=person, collection=collection, type=type,
                                           taken_after=taken_after, taken_before=taken_before, offset=offset)))
        found = []
        for m in LIBRARY:
            d = m["taken_at_local"][:10]
            if person and m["_person"] != person or collection and m["_album"] != collection:
                continue
            if taken_after and d < taken_after or taken_before and d >= taken_before:
                continue
            if type == "video" and m["type"] != "video":
                continue
            if query.startswith("month:"):
                mo, dy = (int(x.split(":")[1]) for x in query.split())
                if (int(d[5:7]), int(d[8:10])) != (mo, dy):
                    continue
            elif query and query.lower() not in m["place"]["name"].lower():
                continue
            found.append({k: v for k, v in m.items() if not k.startswith("_")})
        out = page(found, limit, offset, total=False)  # PhotoPrism gives no total here
        if out.pop("truncated"):
            out["next_offset"] = offset + limit
        return out

    return srv


@pytest.fixture
def calls():
    return []


@pytest.fixture
def photos(calls):
    return ph.Photos(ph.server_connect(fake_photoprism(calls)))


def run(coro):
    return asyncio.run(coro)


def test_person_and_year_summary(photos, calls):
    out = run(photos.search(None, "carmen", None, 2019, None, None, "any"))
    assert out["criteria"] == {"person": "Carmen", "year": 2019, "taken_after": "2019-01-01", "taken_before": "2020-01-01"}
    assert out["count"] == 120 and out["count_is_lower_bound"] is False
    assert out["first_taken"] == out["last_taken"] == "2019-07-01"
    assert out["top_places"] == [{"place": "Llanes, Asturias", "count": 120}]
    assert len(out["examples"]) <= ph.SAMPLES
    searches = [c[1] for c in calls if c[0] == "search_media"]
    assert {s["person"] for s in searches} == {"j1"} and [s["offset"] for s in searches] == [0, 50, 100]


def test_exact_name_beats_longer_one(photos):
    assert run(photos.search(None, "ines", None, None, None, None, "any"))["criteria"]["person"] == "Inés"


def test_unknown_and_ambiguous_names(photos):
    with pytest.raises(ToolError, match="Unknown person"):
        run(photos.search(None, "abuela", None, None, None, None, "any"))
    with pytest.raises(ToolError, match="Ambiguous album"):
        run(photos.search(None, None, "semana santa", None, None, None, "any"))


def test_album_with_videos_and_text(photos, calls):
    out = run(photos.search(None, None, "asturias", None, None, None, "any"))
    assert out["criteria"]["album"] == "Asturias Octubre" and (out["photos"], out["videos"]) == (2, 1)
    vids = run(photos.search(None, None, "asturias", None, None, None, "video"))
    assert vids["count"] == 1 and [c for c in calls if c[0] == "search_media"][-1][1]["type"] == "video"
    madrid = run(photos.search("madrid", None, None, None, None, None, "any"))
    assert madrid["count"] == 10 and madrid["first_taken"].startswith("2020")


def test_by_year_only_when_several_years(photos):
    out = run(photos.search(None, "carmen", None, None, None, None, "any"))
    assert out["by_year"] == {2019: 120, 2020: 10} or out["by_year"] == {"2019": 120, "2020": 10}


def test_scan_limit_marks_lower_bound(photos, monkeypatch):
    monkeypatch.setattr(ph, "SCAN_LIMIT", 100)
    out = run(photos.search(None, "carmen", None, None, None, None, "any"))
    assert out["count"] == 100 and out["count_is_lower_bound"] is True


def test_requires_a_criterion_and_valid_dates(photos):
    with pytest.raises(ToolError, match="at least one criterion"):
        run(photos.search("  ", None, None, None, None, None, "any"))
    with pytest.raises(ToolError, match="date like"):
        run(photos.search(None, None, None, None, "ayer", None, "any"))


def test_one_session_per_question(calls):
    opened = []
    inner = ph.server_connect(fake_photoprism(calls))

    def counting():
        opened.append(1)
        return inner()

    run(ph.Photos(counting).search(None, "carmen", None, 2019, None, None, "any"))  # people + 3 pages
    assert len(opened) == 1


def test_albums_and_people(photos):
    albums = run(photos.albums(None))
    assert albums["total"] == 3 and albums["albums"][0]["title"] == "Playa Semana Santa"  # newest first
    people = run(photos.people(None))
    assert [p["name"] for p in people["people"]] == ["Carmen", "Inés", "Inesa"]


def test_on_this_day_groups_by_local_date(photos):
    out = run(photos.on_this_day(11, 2))
    assert out["years"] == [{"year": 2016, "count": 1, "places": ["Pampaneira, Andalucía"]},
                            {"year": 2006, "count": 1, "places": ["Paris, Île-de-France"]}]
    assert run(photos.on_this_day(None, None, today=date(2026, 11, 2)))["date"] == "02/11"


def test_photoprism_errors_become_tool_errors():
    srv = MCPServer(name="photoprism")

    @srv.tool(name="list_people")
    def list_people(limit: int = 20, offset: int = 0) -> dict:
        raise ToolError("people need the people scope")

    p = ph.Photos(ph.server_connect(srv))
    with pytest.raises(ToolError, match="PhotoPrism: .*people scope"):
        run(p.people(None))


def test_registered_through_mcp(tmp_path, calls):
    env = {"FAMILIA_MCP_ENV_FILE": str(tmp_path / "none.env"), "PHOTOPRISM_URL": "http://photos:2342",
           "PHOTOPRISM_TOKEN": "t"}
    server = build_server(env, photos_target=fake_photoprism(calls))

    async def go():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == {"photos_search", "photos_albums", "photos_people", "photos_on_this_day"}
            assert all(t.annotations.read_only_hint for t in tools.values())
            return structured(await client.call_tool("photos_search", {"person": "Carmen", "year": 2020}))

    assert asyncio.run(go())["count"] == 10
