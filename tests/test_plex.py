import asyncio
import time

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from conftest import structured
from familia_mcp import plex as px
from familia_mcp.config import PlexConfig
from familia_mcp.server import build_server

OWNER, KIDS = "owner-token", "kids-token"
NOW = int(time.time())

ACCOUNTS = [{"id": 0, "name": ""}, {"id": 1, "name": "owner"}, {"id": 424242, "name": "Kids"}]
DEVICES = [{"id": 164, "name": "TV salón"}, {"id": 7, "name": "iPad"}]
MOVIE = {
    "type": "movie", "title": "Toy Story", "year": 1995, "duration": 4860000, "viewCount": 2,
    "summary": "A cowboy doll is profoundly threatened " * 20, "audienceRating": 9.2,
    "contentRating": "G", "librarySectionTitle": "Movies",
    "Genre": [{"tag": "Animation"}, {"tag": "Comedy"}], "Director": [{"tag": "John Lasseter"}],
    "Role": [{"tag": "Tom Hanks"}, {"tag": "Tim Allen"}],
}
SHOW = {"type": "show", "title": "Bluey", "year": 2018, "childCount": 3, "leafCount": 150, "viewedLeafCount": 40}
EPISODE = {"type": "episode", "grandparentTitle": "Full House", "parentIndex": 2, "index": 5,
           "title": "Lust in the Dust", "viewOffset": 300000, "duration": 1500000, "lastViewedAt": NOW - 60}


class FakePlex:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.method == "GET"
        if self.status != 200:
            return httpx.Response(self.status)
        token = request.headers["X-Plex-Token"]
        path, params = request.url.path, dict(request.url.params)

        def mc(**kw):
            return httpx.Response(200, json={"MediaContainer": kw})

        if path == "/accounts":
            return mc(Account=ACCOUNTS)
        if path == "/devices":
            return mc(Device=DEVICES)
        if path == "/hubs/search":
            return mc(Hub=[{"type": "movie", "Metadata": [MOVIE]}, {"type": "show", "Metadata": [SHOW]},
                           {"type": "actor", "Metadata": [{"title": "Tom Hanks"}]}])
        if path == "/library/recentlyAdded":
            return mc(Metadata=[
                {"type": "season", "parentTitle": "Bluey", "title": "Season 3", "leafCount": 4, "addedAt": NOW - 3600},
                dict(MOVIE, addedAt=NOW - 7200),
            ])
        if path == "/library/onDeck":
            if token == KIDS:
                return mc(Metadata=[EPISODE, EPISODE])  # duplicated across libraries
            return mc(Metadata=[dict(MOVIE, viewOffset=2430000, lastViewedAt=NOW - 100)])
        if path == "/status/sessions/history/all":
            rows = [
                dict(EPISODE, accountID=424242, viewedAt=NOW - 600, deviceID=164),
                dict(MOVIE, accountID=1, viewedAt=NOW - 7200, deviceID=7),
                dict(MOVIE, accountID=1, viewedAt=NOW - 10 * 86400, deviceID=7),  # outside 24h/7d
            ]
            if "accountID" in params:
                rows = [r for r in rows if str(r["accountID"]) == params["accountID"]]
            return mc(Metadata=rows)
        if path == "/status/sessions":
            return mc(Metadata=[dict(EPISODE, User={"title": "Kids"}, Player={"title": "TV salón", "state": "playing"})])
        return httpx.Response(404)


@pytest.fixture
def fake_plex():
    return FakePlex()


@pytest.fixture
def plex(fake_plex):
    cfg = PlexConfig(url="http://pidata:32400", token=OWNER, user_tokens={"Kids": KIDS})
    return px.Plex(px.PlexClient(cfg, transport=httpx.MockTransport(fake_plex.handler)), cfg.user_tokens)


def run(coro):
    return asyncio.run(coro)


def test_search_filters_kinds_and_shapes_results(plex, fake_plex):
    out = run(plex.search("toy", "any", 5))
    assert [r.get("title") for r in out["results"]] == ["Toy Story", "Bluey"]  # actor hub ignored
    movie = out["results"][0]
    assert movie["duration_min"] == 81 and movie["watched_by_owner"] is True
    assert movie["actors"] == ["Tom Hanks", "Tim Allen"] and movie["genres"] == ["Animation", "Comedy"]
    assert len(movie["summary"]) <= px.SUMMARY_CHARS and movie["summary"].endswith("…")
    assert out["results"][1]["episodes_watched_by_owner"] == 40
    assert dict(fake_plex.requests[0].url.params) == {"query": "toy", "limit": "5"}
    assert [r["title"] for r in run(plex.search("toy", "movie", 5))["results"]] == ["Toy Story"]


def test_search_rejects_empty(plex):
    with pytest.raises(ToolError):
        run(plex.search("  ", "any", 5))


def test_recently_added(plex):
    out = run(plex.recently_added("any", 8))
    assert out["items"][0]["type"] == "season" and out["items"][0]["added"]
    assert out["items"][0]["show"] == "Bluey" and out["items"][0]["new_episodes"] == 4
    assert out["items"][1]["title"] == "Toy Story"
    assert [i["type"] for i in run(plex.recently_added("movie", 8))["items"]] == ["movie"]


def test_continue_watching_uses_each_accounts_token(plex, fake_plex):
    kids = run(plex.continue_watching("los kids"))
    assert kids["account"] == "Kids" and kids["count"] == 1  # deduplicated
    assert kids["items"][0] == {"type": "episode", "show": "Full House", "episode": "S02E05",
                                "episode_title": "Lust in the Dust", "progress_pct": 20,
                                "last_viewed": kids["items"][0]["last_viewed"]}
    assert fake_plex.requests[-1].headers["X-Plex-Token"] == KIDS
    owner = run(plex.continue_watching(None))
    assert owner["account"] == "owner" and owner["items"][0]["progress_pct"] == 50
    assert fake_plex.requests[-1].headers["X-Plex-Token"] == OWNER


def test_continue_watching_without_user_token_never_falls_back_to_owner(fake_plex):
    cfg = PlexConfig(url="http://pidata:32400", token=OWNER, user_tokens={})
    p = px.Plex(px.PlexClient(cfg, transport=httpx.MockTransport(fake_plex.handler)), {})
    with pytest.raises(ToolError, match="No access token"):
        run(p.continue_watching("Kids"))
    assert not any(r.url.path == "/library/onDeck" for r in fake_plex.requests)


def test_unknown_account(plex):
    with pytest.raises(ToolError, match="Unknown Plex account"):
        run(plex.continue_watching("abuela"))


def test_watch_history_all_and_per_account(plex, fake_plex):
    out = run(plex.watch_history(None, "24h"))
    assert out["total_views"] == 2 and out["views_by_account"] == {"Kids": 1, "owner": 1}
    assert out["recent"][0]["device"] == "TV salón" and out["recent"][0]["account"] == "Kids"
    kids = run(plex.watch_history("Kids", "7d"))
    assert kids["account"] == "Kids" and kids["total_views"] == 1
    hist = [r for r in fake_plex.requests if r.url.path.endswith("history/all")][-1]
    assert hist.url.params["accountID"] == "424242"
    assert run(plex.watch_history(None, "30d"))["total_views"] == 3


def test_now_playing(plex):
    out = run(plex.now_playing())
    assert out["sessions"][0]["account"] == "Kids" and out["sessions"][0]["state"] == "playing"
    assert out["sessions"][0]["device"] == "TV salón"


def test_client_refuses_paths_outside_allowlist(plex):
    for path in ("/library/sections/1/refresh", "/:/scrobble", "/library/metadata/1"):
        with pytest.raises(ToolError, match="not allowed"):
            run(plex.client.get(path))


@pytest.mark.parametrize("status,msg", [(401, "rejected"), (500, "HTTP 500")])
def test_http_errors(plex, fake_plex, status, msg):
    fake_plex.status = status
    with pytest.raises(ToolError, match=msg):
        run(plex.now_playing())


def test_config_parsing():
    assert PlexConfig.from_env({}) is None
    cfg = PlexConfig.from_env({"PLEX_URL": "http://x:32400/", "PLEX_TOKEN": "t", "PLEX_USER_TOKENS": "Kids=k; Abuelos = a"})
    assert cfg.url == "http://x:32400" and cfg.user_tokens == {"Kids": "k", "Abuelos": "a"}
    with pytest.raises(ValueError):
        PlexConfig.from_env({"PLEX_URL": "http://x", "PLEX_TOKEN": "t", "PLEX_USER_TOKENS": "Kids"})
    with pytest.raises(ValueError):
        PlexConfig.from_env({"PLEX_URL": "x:32400", "PLEX_TOKEN": "t"})


def test_tools_registered_through_mcp(tmp_path, fake_plex):
    env = {"FAMILIA_MCP_ENV_FILE": str(tmp_path / "none.env"), "PLEX_URL": "http://pidata:32400",
           "PLEX_TOKEN": OWNER, "PLEX_USER_TOKENS": f"Kids={KIDS}"}
    server = build_server(env, plex_transport=httpx.MockTransport(fake_plex.handler))

    async def go():
        async with Client(server) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert set(tools) == {"plex_search", "plex_recently_added", "plex_continue_watching",
                                  "plex_watch_history", "plex_now_playing"}
            assert all(t.annotations.read_only_hint for t in tools.values())
            assert "Kids" in tools["plex_continue_watching"].description
            res = await client.call_tool("plex_continue_watching", {"account": "Kids"})
            return structured(res)

    assert asyncio.run(go())["items"][0]["show"] == "Full House"
