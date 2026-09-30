"""News layer: all network and LLM calls are mocked."""
import json

import httpx

from conftest import b, make_state
from fantasy_ai.lineup import optimize
from fantasy_ai.news import adjust
from fantasy_ai.news.cache import NewsCache, PoliteClient
from fantasy_ai.news.classify import Signal, classify, parse_signals
from fantasy_ai.news.pipeline import enrich
from fantasy_ai.news.sources import (Buzz, EspnPlayerNews, RedditSource, RssSource, SleeperTrending,
                                     NewsItem, sleeper_buzz, x_source)
from fantasy_ai.waivers import rank_free_agents

RSS = """<rss><channel>
<item><title>Ready Rex expected to start Sunday</title><description>Coach says Rex gets the bulk of carries.</description>
<link>https://example.com/rex</link><pubDate>2026-09-30</pubDate></item>
<item><title>Unrelated player news</title><description>nothing</description><link>x</link></item>
</channel></rss>"""

ESPN_NEWS = {"feed": [{"headline": "Quinn Arm questionable with ankle", "story": "Limited in practice.",
                       "published": "2026-09-30T15:00:00Z", "links": {"web": {"href": "https://espn.com/n/1"}}}]}
REDDIT = {"data": {"children": [{"data": {"title": "Is Waiver Wonder the real deal?", "selftext": "",
                                          "permalink": "/r/ff/1", "created_utc": 1}}]}}
SLEEPER_ADDS = [{"player_id": "s901", "count": 50000}]
SLEEPER_DROPS = [{"player_id": "s402", "count": 9000}]
SLEEPER_DIR = {"s901": {"full_name": "Waiver Wonder", "position": "TE"},
               "s402": {"full_name": "Tiny Tim", "position": "TE"}}


def mock_client(tmp_path):
    def handler(req: httpx.Request):
        u = str(req.url)
        if "rss" in u or "feed" in u:
            return httpx.Response(200, text=RSS)
        if "news/players" in u:
            return httpx.Response(200, json=ESPN_NEWS if "playerId=101" in u else {"feed": []})
        if "reddit" in u:
            return httpx.Response(200, json=REDDIT)
        if "trending/add" in u:
            return httpx.Response(200, json=SLEEPER_ADDS)
        if "trending/drop" in u:
            return httpx.Response(200, json=SLEEPER_DROPS)
        if "sleeper.app/v1/players/nfl" in u:
            return httpx.Response(200, json=SLEEPER_DIR)
        return httpx.Response(404)
    return PoliteClient(NewsCache(tmp_path / "cache"), transport=httpx.MockTransport(handler), sleep=lambda s: None)


def fake_llm(prompt):
    rows = json.loads(prompt[prompt.index("["):])
    out = []
    for r in rows:
        if "questionable" in r["title"]:
            out.append({"item": r["item"], "player": r["player"], "status_change": "questionable",
                        "role_change": 0, "usage_note": "ankle, limited practice", "sentiment": -0.4, "confidence": 0.9})
        elif "start" in r["title"]:
            out.append({"item": r["item"], "player": r["player"], "status_change": None, "role_change": 1,
                        "usage_note": "bulk of carries", "sentiment": 0.6, "confidence": 0.8})
    return json.dumps(out)


def test_sources_parse_and_cache(tmp_path, demo_state):
    c = mock_client(tmp_path)
    players = list(demo_state.roster) + demo_state.free_agents
    rss = RssSource("Test RSS", "https://example.com/rss", c).fetch(players)
    assert [i.player_name for i in rss] == ["Ready Rex"]
    espn = EspnPlayerNews(c).fetch([p for p in players if p.player_id == 101])
    assert espn[0].title.startswith("Quinn Arm") and espn[0].url == "https://espn.com/n/1"
    assert RedditSource(c).fetch(players)[0].player_name == "Waiver Wonder"
    assert x_source().fetch(players) == []           # stub, no scraping
    buzz = SleeperTrending(c).buzz(players)
    assert buzz[901].add_rank == 1 and buzz[402].drop_rank == 1
    # second call served from cache (the mock would still answer, so check the file exists)
    assert list((tmp_path / "cache").glob("*.json"))


def test_parse_signals_rejects_garbage():
    items = [NewsItem("A", "t", "s", "u", "ESPN", "2026-09-30", 1)]
    assert parse_signals("not json", items) == []
    sig = parse_signals('[{"item": 0, "status_change": "BENCH HIM", "role_change": 7, "sentiment": 9, "confidence": 2}, {"item": 5}]', items)
    assert len(sig) == 1 and sig[0].status_change is None and sig[0].role_change == 0
    assert sig[0].sentiment == 1 and sig[0].confidence == 1 and sig[0].source_url == "u"


def test_adjust_caps_and_reasons():
    p = make_state([b.entry(b.player(1, "Hype Guy", "WR", 10), 4)]).roster[0]
    hype = [Signal(1, "Hype Guy", None, 1, "more snaps", 1.0, 1.0, "ESPN", "u", "2026-09-30")] * 5
    a = adjust.compute(p, hype)
    assert a.multiplier <= (1 + adjust.ROLE_CAP) * (1 + adjust.SENT_CAP) + 1e-3
    assert any("role up" in r for r in a.reasons)
    out = adjust.compute(p, [Signal(1, "Hype Guy", "out", 0, "", -1, 0.9, "ESPN", "u", "2026-09-30")])
    assert out.multiplier == 0 and out.news_out and "OUT (ESPN, 2026-09-30)" in out.reasons[0]
    weak = adjust.compute(p, [Signal(1, "Hype Guy", "out", 0, "", 0, 0.55, "Reddit", "u", "")])
    assert not weak.news_out and weak.multiplier == 0.4


def test_questionable_starter_loses_to_close_backup():
    e = [b.entry(b.player(1, "Q Star", "QB", 20), 0), b.entry(b.player(2, "Q Safe", "QB", 18), 20)]
    s = make_state(e)
    adjust.apply(s, [Signal(1, "Q Star", "questionable", 0, "hamstring", 0, 1.0, "ESPN", "u", "2026-09-30")])
    res = optimize(s)
    assert res.target[2] == 0 and res.target[1] == 20
    reasons = " ".join(m.reason for m in res.moves)
    assert "Q Star listed questionable (ESPN, 2026-09-30)" in reasons and "ESPN 20.0" not in reasons


def test_news_out_benches_even_if_espn_active():
    e = [b.entry(b.player(1, "Q Star", "QB", 20), 0), b.entry(b.player(2, "Q Two", "QB", 8), 20)]
    s = make_state(e)
    adjust.apply(s, [Signal(1, "Q Star", "out", 0, "", -1, 0.95, "NBC Rotoworld", "u", "2026-09-30")])
    assert optimize(s).target[1] == 20


def test_buzz_raises_waiver_value(demo_state):
    before = {w.add.name: w.add_value for w in rank_free_agents(demo_state)}
    adjust.apply(demo_state, [], {901: Buzz(add_rank=1, add_count=50000)})
    after = {w.add.name: w.add_value for w in rank_free_agents(demo_state)}
    assert after["Waiver Wonder"] > before["Waiver Wonder"]
    assert after["Waiver Wonder"] <= before["Waiver Wonder"] * (1 + adjust.BUZZ_CAP) + 0.2


def test_enrich_end_to_end(tmp_path, demo_state):
    adjs = enrich(demo_state, fake_llm, client=mock_client(tmp_path),
                  sources=[EspnPlayerNews(mock_client(tmp_path)), RssSource("Test RSS", "https://example.com/rss", mock_client(tmp_path))])
    quinn = demo_state.find_player("Quinn Arm")
    rex = demo_state.find_player("Ready Rex")
    assert quinn.base_proj_week == 20.0 and quinn.proj_week < 20.0
    assert rex.proj_week > 12.0 and rex.ros_multiplier > 1
    assert any("most-added" in r for r in demo_state.find_player("Waiver Wonder").reasons)
    assert 101 in adjs
