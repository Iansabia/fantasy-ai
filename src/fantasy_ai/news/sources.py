"""News and buzz sources. All free, all read-only, all cached and rate limited.

Live status: none of these endpoints has been exercised from this code yet (tests use mocks).
URLs marked [unverified] are best-known public endpoints and should be checked once.

X and Instagram have no usable free API and are NOT scraped: they are stubs that can read a
user-provided JSON list of items, or be wired to a paid API later.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Protocol

from ..models import Player
from .cache import PoliteClient

# ESPN fantasy player news (same host espn-api's news_get uses) [unverified from this code]
ESPN_PLAYER_NEWS = "https://site.api.espn.com/apis/fantasy/v2/games/ffl/news/players"
SLEEPER_TRENDING = "https://api.sleeper.app/v1/players/nfl/trending/{kind}"
SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"   # ~5 MB, cached for a day
REDDIT_NEW = "https://www.reddit.com/r/fantasyfootball/new.json"

# [unverified] feed URLs; edit in config if any move.
DEFAULT_RSS = {
    "NBC Rotoworld": "https://www.nbcsports.com/fantasy/football/player-news/rss",
    "RotoBaller": "https://www.rotoballer.com/category/nfl/feed",
    "FantasyPros": "https://www.fantasypros.com/nfl/rss/news.xml",
}


@dataclass
class NewsItem:
    player_name: str
    title: str
    summary: str
    url: str
    source: str
    published: str = ""        # ISO date if known
    player_id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Buzz:
    add_rank: int | None = None      # 1 = most-added in Sleeper over the lookback window
    add_count: int = 0
    drop_rank: int | None = None
    drop_count: int = 0
    news_count: int = 0


class Source(Protocol):
    name: str

    def fetch(self, players: list[Player]) -> list[NewsItem]: ...


def _norm(name: str) -> str:
    return re.sub(r"[^a-z ]", "", name.lower().replace(".", "")).strip()


def match_players(text: str, players: Iterable[Player]) -> list[Player]:
    """Players whose full name appears in the text (D/ST skipped: team names are ambiguous)."""
    t = " " + _norm(text) + " "
    return [p for p in players if p.position != "D/ST" and f" {_norm(p.name)} " in t]


class EspnPlayerNews:
    name = "ESPN"

    def __init__(self, client: PoliteClient, per_player: int = 5):
        self.client, self.per_player = client, per_player

    def fetch(self, players: list[Player]) -> list[NewsItem]:
        out = []
        for p in players:
            if p.position == "D/ST":
                continue
            try:
                data = self.client.get(ESPN_PLAYER_NEWS, ttl=3600, params={"playerId": p.player_id, "limit": self.per_player})
            except Exception:
                continue
            for f in (data or {}).get("feed", [])[: self.per_player]:
                out.append(NewsItem(
                    player_name=p.name, player_id=p.player_id, source=self.name,
                    title=f.get("headline", ""), summary=f.get("story") or f.get("description", ""),
                    url=((f.get("links") or {}).get("web") or {}).get("href", ""),
                    published=f.get("published") or f.get("lastModified", ""),
                ))
        return out


class RssSource:
    def __init__(self, name: str, url: str, client: PoliteClient):
        self.name, self.url, self.client = name, url, client

    def fetch(self, players: list[Player]) -> list[NewsItem]:
        try:
            xml = self.client.get(self.url, ttl=1800, as_json=False)
            root = ET.fromstring(xml)
        except Exception:
            return []
        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            desc = re.sub(r"<[^>]+>", " ", item.findtext("description") or "").strip()
            for p in match_players(f"{title} {desc}", players):
                out.append(NewsItem(p.name, title, desc[:600], item.findtext("link") or "", self.name,
                                    item.findtext("pubDate") or "", p.player_id))
        return out


class RedditSource:
    name = "Reddit r/fantasyfootball"

    def __init__(self, client: PoliteClient, limit: int = 50):
        self.client, self.limit = client, limit

    def fetch(self, players: list[Player]) -> list[NewsItem]:
        try:
            data = self.client.get(REDDIT_NEW, ttl=1800, params={"limit": self.limit})
        except Exception:
            return []
        out = []
        for child in (data or {}).get("data", {}).get("children", []):
            d = child.get("data", {})
            text = f"{d.get('title', '')} {d.get('selftext', '')[:400]}"
            for p in match_players(text, players):
                out.append(NewsItem(p.name, d.get("title", ""), d.get("selftext", "")[:400],
                                    "https://www.reddit.com" + d.get("permalink", ""), self.name,
                                    str(d.get("created_utc", "")), p.player_id))
        return out


class UserListSource:
    """Stub for sources with no free API (X, Instagram). Reads items a user saved to a JSON
    file: [{"player_name", "title", "summary", "url", "published"}]. Never scrapes."""

    def __init__(self, name: str, path: str | Path | None = None):
        self.name, self.path = name, Path(path) if path else None

    def fetch(self, players: list[Player]) -> list[NewsItem]:
        if not self.path or not self.path.exists():
            return []
        names = {_norm(p.name): p for p in players}
        out = []
        for row in json.loads(self.path.read_text()):
            p = names.get(_norm(row.get("player_name", "")))
            if p:
                out.append(NewsItem(p.name, row.get("title", ""), row.get("summary", ""), row.get("url", ""),
                                    self.name, row.get("published", ""), p.player_id))
        return out


def x_source(path=None) -> UserListSource:
    return UserListSource("X", path)


def instagram_source(path=None) -> UserListSource:
    return UserListSource("Instagram", path)


class SleeperTrending:
    """Sleeper trending adds/drops: the strongest free 'buzz' signal."""

    def __init__(self, client: PoliteClient, lookback_hours: int = 24, limit: int = 50):
        self.client, self.lookback, self.limit = client, lookback_hours, limit

    def buzz(self, players: list[Player]) -> dict[int, Buzz]:
        try:
            adds = self.client.get(SLEEPER_TRENDING.format(kind="add"), ttl=3600,
                                   params={"lookback_hours": self.lookback, "limit": self.limit})
            drops = self.client.get(SLEEPER_TRENDING.format(kind="drop"), ttl=3600,
                                    params={"lookback_hours": self.lookback, "limit": self.limit})
            directory = self.client.get(SLEEPER_PLAYERS, ttl=86400)
        except Exception:
            return {}
        return sleeper_buzz(adds, drops, directory, players)


def sleeper_buzz(adds: list, drops: list, directory: dict, players: list[Player]) -> dict[int, Buzz]:
    """Map Sleeper ids -> our players by normalized full name (+ position)."""
    by_name = {(_norm(p.name), p.position): p for p in players}
    out: dict[int, Buzz] = {}

    def lookup(sid):
        d = directory.get(str(sid)) or {}
        name = d.get("full_name") or f"{d.get('first_name', '')} {d.get('last_name', '')}"
        return by_name.get((_norm(name), d.get("position", "")))

    for rank, row in enumerate(adds or [], 1):
        p = lookup(row.get("player_id"))
        if p:
            b = out.setdefault(p.player_id, Buzz())
            b.add_rank, b.add_count = rank, int(row.get("count", 0))
    for rank, row in enumerate(drops or [], 1):
        p = lookup(row.get("player_id"))
        if p:
            b = out.setdefault(p.player_id, Buzz())
            b.drop_rank, b.drop_count = rank, int(row.get("count", 0))
    return out


def default_sources(client: PoliteClient, rss: dict[str, str] | None = None,
                    x_path=None, instagram_path=None) -> list:
    srcs: list = [EspnPlayerNews(client)]
    srcs += [RssSource(n, u, client) for n, u in (rss or DEFAULT_RSS).items()]
    srcs += [RedditSource(client), x_source(x_path), instagram_source(instagram_path)]
    return srcs
