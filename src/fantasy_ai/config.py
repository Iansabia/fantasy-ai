"""League config (YAML) and per-profile ESPN cookies (from a gitignored .env only)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "leagues.yaml"
EXAMPLE_CONFIG = ROOT / "config" / "leagues.example.yaml"


@dataclass
class Settings:
    auto_lineup: bool = False
    waivers_need_approval: bool = True
    trades_propose_only: bool = True


@dataclass
class LeagueConfig:
    name: str
    owner: str
    league_id: int
    team_id: int
    year: int
    auth_profile: str
    fixture: str | None = None      # offline mode: path to a fixture dir, relative to repo root
    display_name: str = ""
    faab: bool = False              # False = waiver priority; claims send bidAmount 0
    ppr: float = 1.0                # scoring for the FantasyCalc market benchmark

    @property
    def label(self) -> str:
        return self.display_name or self.name

    @property
    def fixture_dir(self) -> Path | None:
        if not self.fixture:
            return None
        p = Path(self.fixture)
        return p if p.is_absolute() else ROOT / p


@dataclass
class Credentials:
    espn_s2: str
    swid: str

    def __repr__(self) -> str:  # never leak secrets through logs or tracebacks
        return "Credentials(espn_s2=***, swid=***)"

    __str__ = __repr__

    def cookies(self) -> dict[str, str]:
        c = {"espn_s2": self.espn_s2}
        if self.swid:
            c["SWID"] = self.swid
        return c


@dataclass
class AppConfig:
    leagues: list[LeagueConfig]
    settings: Settings = field(default_factory=Settings)
    source: Path | None = None
    notify: dict = field(default_factory=dict)   # {"backend": ..., "to": {owner: ENV_VAR_NAME}}
    factors: dict = field(default_factory=dict)  # weights/caps, see factors/score.py DEFAULTS
    pacing: dict = field(default_factory=dict)   # see pacing.py DEFAULTS
    waivers: dict = field(default_factory=dict)  # see waivers.py DEFAULTS
    hype: dict = field(default_factory=dict)     # label -> [ros mult, future mult], see hype.py MULTS

    def get(self, name: str) -> LeagueConfig:
        for lg in self.leagues:
            if lg.name.lower() == name.lower() or str(lg.league_id) == str(name):
                return lg
        known = ", ".join(lg.name for lg in self.leagues)
        raise KeyError(f"No league named '{name}'. Known: {known}")


def load_config(path: str | Path | None = None) -> AppConfig:
    """Order: explicit path, $FAI_CONFIG path, $LEAGUES_YAML contents (CI secret),
    config/leagues.yaml, then the example."""
    if path is None and os.environ.get("LEAGUES_YAML"):
        return _parse(yaml.safe_load(os.environ["LEAGUES_YAML"]) or {}, None)
    if path is None:
        env_path = os.environ.get("FAI_CONFIG")
        path = Path(env_path) if env_path else (DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else EXAMPLE_CONFIG)
    path = Path(path)
    return _parse(yaml.safe_load(path.read_text()) or {}, path)


def _parse(data: dict, path: Path | None) -> AppConfig:
    leagues = [
        LeagueConfig(
            name=str(l["name"]), owner=str(l.get("owner", "ian")),
            league_id=int(l["league_id"]), team_id=int(l["team_id"]), year=int(l["year"]),
            auth_profile=str(l.get("auth_profile", l.get("owner", "ian"))),
            fixture=l.get("fixture"),
            display_name=str(l.get("display_name", "")),
            faab=bool(l.get("faab", False)),
            ppr=float(l.get("ppr", 1.0)),
        )
        for l in data.get("leagues", [])
    ]
    s = data.get("settings", {}) or {}
    settings = Settings(
        auto_lineup=bool(s.get("auto_lineup", False)),
        # safety settings can only be tightened, never loosened, from YAML
        waivers_need_approval=True,
        trades_propose_only=True,
    )
    return AppConfig(leagues=leagues, settings=settings, source=path, notify=data.get("notify") or {},
                     factors=data.get("factors") or {}, pacing=data.get("pacing") or {},
                     waivers=data.get("waivers") or {}, hype=data.get("hype") or {})


def load_credentials(profile: str, env_file: str | Path | None = None) -> Credentials | None:
    """Read ESPN_S2_<PROFILE> and SWID_<PROFILE>. Returns None if missing. Never prints them."""
    load_dotenv(env_file or ROOT / ".env", override=False)
    key = profile.upper().replace("-", "_")
    s2 = os.environ.get(f"ESPN_S2_{key}", "").strip()
    swid = os.environ.get(f"SWID_{key}", "").strip()
    if not s2:
        return None
    return Credentials(espn_s2=s2, swid=swid)
