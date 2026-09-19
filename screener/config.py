"""Reads filters.toml and searches.toml into frozen dataclasses.

Config is TOML rather than Python constants because the weekly tuning pass —
another blocked employer, another clearance phrasing — should never require
opening a .py file or restarting anything.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

FILTERS_FILE = "filters.toml"
SEARCHES_FILE = "searches.toml"
APP_NAME = "job-triage"


class HomeNotFound(Exception):
    """Raised when the home folder has no config yet: `screener init` makes one."""


def user_home() -> Path:
    """Where your config and data live: SCREENER_HOME if it's set, otherwise
    %APPDATA%\\job-triage on Windows and ~/.config/job-triage elsewhere.

    Deliberately not the working directory, and not next to the code. The first
    means the command only works from one folder, and running it from anywhere
    else would quietly open a second, empty jobs.db. The second breaks the moment
    the package is installed somewhere other than a clone.
    """
    override = os.environ.get("SCREENER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP_NAME


def home() -> Path:
    """The home folder, once `screener init` has set it up."""
    path = user_home()
    if not (path / FILTERS_FILE).is_file():
        raise HomeNotFound(f'no config in {path}; run "screener init" to create it')
    return path


@dataclass(frozen=True)
class KeywordFlagRule:
    """A soft flag: annotate the job, don't kill it."""

    flag: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class RemoteRules:
    """The phrase lists that decide whether a job the board calls remote really is.
    `_really_remote` in filters.py applies them, in the order documented there."""

    contradicted_by: tuple[str, ...]
    title_contradicted_by: tuple[str, ...]
    stated_by: tuple[str, ...]
    loosely_stated_by: tuple[str, ...]


AMBIGUOUS_MODES = ("classify", "kill", "keep")


@dataclass(frozen=True)
class GateRules:
    """The relevance gate's title patterns. `gate.py` applies them; deny wins."""

    allow: tuple[str, ...]
    deny: tuple[str, ...]
    ambiguous: str


@dataclass(frozen=True)
class FilterConfig:
    blocked_companies: tuple[str, ...]
    kill_keywords: tuple[str, ...]
    title_kill: tuple[str, ...]
    negation_before: tuple[str, ...]
    negation_after: tuple[str, ...]
    window_chars: int
    primary_locations: frozenset[str]
    secondary_locations: frozenset[str]
    secondary_tag: str
    foreign_markers: frozenset[str]
    keep_remote_us: bool
    low_pay_below: float
    contract_job_types: frozenset[str]
    keyword_flags: tuple[KeywordFlagRule, ...]
    remote: RemoteRules
    gate: GateRules
    # Whether the [govcon] group is merged into the two lists above.
    govcon_enabled: bool = False


@dataclass(frozen=True)
class Search:
    term: str
    location: str
    is_remote: bool = False


PRIMARY_MARKET = "primary"
SECONDARY_MARKET = "secondary"
MARKETS = (PRIMARY_MARKET, SECONDARY_MARKET)


@dataclass(frozen=True)
class Location:
    """One [[locations]] block: where to search, and which location tokens keep
    what it finds. The two live together so adding a place can't pull jobs the
    location rule then kills."""

    name: str
    remote: bool = False
    market: str = PRIMARY_MARKET
    match: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdzunaConfig:
    """The [adzuna] block plus the two keys from .env. `enabled` is the file's
    opinion; `configured` is whether the keys are actually there."""

    enabled: bool
    app_id: str
    app_key: str
    country: str
    distance_km: int
    results_per_page: int
    max_days_old: int
    category: str
    what_exclude: str

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_key)


@dataclass(frozen=True)
class SearchConfig:
    sites: tuple[str, ...]
    results_wanted: int
    hours_old: int
    country_indeed: str
    adzuna: AdzunaConfig
    titles: tuple[str, ...]
    titles_per_search: int
    locations: tuple[Location, ...]
    searches: tuple[Search, ...]


def load_filters(path: Path, searches: SearchConfig) -> FilterConfig:
    """The filter rules, plus the two things searches.toml contributes: the
    titles you search for are let through the gate, and each location's match
    tokens decide which postings the location rule keeps."""
    raw = _read_toml(path)
    keywords = raw["keywords"]
    location = raw["location"]
    flags = raw["flags"]
    remote = raw["remote"]
    gate = raw["gate"]
    if gate["ambiguous"] not in AMBIGUOUS_MODES:
        raise ValueError(f"[gate] ambiguous must be one of {', '.join(AMBIGUOUS_MODES)}, not {gate['ambiguous']!r}")

    # The govcon group sits first when it's on, so its employers and keywords
    # get the credit in `stats` exactly as they did before it was a switch.
    govcon = raw.get("govcon", {})
    govcon_on = bool(govcon.get("enabled", False))
    blocklist = [*govcon.get("blocklist", [])] if govcon_on else []
    kill = [*govcon.get("kill", [])] if govcon_on else []

    return FilterConfig(
        blocked_companies=tuple(blocklist + raw["companies"]["blocklist"]),
        kill_keywords=tuple(kill + keywords["kill"]),
        title_kill=tuple(keywords.get("title_kill", [])),
        negation_before=tuple(keywords["negation_before"]),
        negation_after=tuple(keywords["negation_after"]),
        window_chars=int(keywords["window_chars"]),
        # Folded here rather than at match time so every lookup downstream is a
        # plain set membership test against already-normalized tokens.
        primary_locations=_token_set(_match_tokens(searches.locations, PRIMARY_MARKET)),
        secondary_locations=_token_set(_match_tokens(searches.locations, SECONDARY_MARKET)),
        secondary_tag=location["secondary_tag"],
        foreign_markers=_token_set(location["foreign_markers"]),
        keep_remote_us=bool(location["keep_remote_us"]),
        low_pay_below=float(flags["salary"]["low_pay_below"]),
        contract_job_types=_token_set(flags["contract"]["job_types"]),
        keyword_flags=tuple(
            KeywordFlagRule(flag=rule["flag"], keywords=tuple(rule["keywords"]))
            for rule in flags["keyword"]
        ),
        remote=RemoteRules(
            contradicted_by=tuple(remote["contradicted_by"]),
            title_contradicted_by=tuple(remote["title_contradicted_by"]),
            stated_by=tuple(remote["stated_by"]),
            loosely_stated_by=tuple(remote["loosely_stated_by"]),
        ),
        gate=GateRules(
            # A title you chose to search for can't then die as ambiguous.
            allow=tuple(gate["allow"]) + searches.titles,
            deny=tuple(gate["deny"]),
            ambiguous=gate["ambiguous"],
        ),
        govcon_enabled=govcon_on,
    )


def load_searches(path: Path) -> SearchConfig:
    raw = _read_toml(path)
    defaults = raw["defaults"]
    search = raw.get("search", {})
    titles = tuple(search.get("titles", []))
    per_search = int(search.get("titles_per_search", 5))
    if per_search < 1:
        raise ValueError("[search] titles_per_search must be 1 or more")
    locations = tuple(_load_location(entry) for entry in raw.get("locations", []))

    return SearchConfig(
        sites=tuple(defaults["sites"]),
        results_wanted=int(defaults["results_wanted"]),
        hours_old=int(defaults["hours_old"]),
        country_indeed=defaults["country_indeed"],
        adzuna=_load_adzuna(raw.get("adzuna", {}), path.parent / ".env"),
        titles=titles,
        titles_per_search=per_search,
        locations=locations,
        searches=build_searches(titles, locations, per_search),
    )


def build_searches(titles: tuple[str, ...], locations: tuple[Location, ...], per_search: int) -> tuple[Search, ...]:
    """Every title in every location, `per_search` titles to a query. Indeed caps
    each query at `results_wanted`, so one query OR-ing twenty titles would
    return a fifth of what four smaller ones do."""
    chunks = [titles[i : i + per_search] for i in range(0, len(titles), per_search)]
    return tuple(
        Search(term=build_term(chunk), location=place.name, is_remote=place.remote)
        for place in locations
        for chunk in chunks
    )


def build_term(titles: tuple[str, ...]) -> str:
    """Indeed's title:(...) operator. A bare or quoted term searches the whole
    posting: "help desk" returned attorneys whose boilerplate said "contact the
    help desk". Only title:() searches the title."""
    parts = (f'"{title}"' if " " in title else title for title in titles)
    return f"title:({' OR '.join(parts)})"


def _load_location(entry: dict) -> Location:
    market = entry.get("market", PRIMARY_MARKET)
    if market not in MARKETS:
        raise ValueError(f"location {entry.get('name')!r}: market must be primary or secondary, not {market!r}")
    return Location(
        name=entry["name"],
        remote=bool(entry.get("remote", False)),
        market=market,
        match=tuple(entry.get("match", [])),
    )


def _match_tokens(locations: tuple[Location, ...], market: str) -> list[str]:
    # Remote locations have no tokens of their own; keep_remote_us governs them.
    return [token for place in locations if place.market == market for token in place.match]


def _load_adzuna(raw: dict, env_file: Path) -> AdzunaConfig:
    """Keys come from .env next to the TOML, never from the TOML itself, so the
    config file stays committable. A missing block means the source is off."""
    load_dotenv(env_file)
    return AdzunaConfig(
        enabled=bool(raw.get("enabled", False)),
        app_id=os.environ.get("ADZUNA_APP_ID", "").strip(),
        app_key=os.environ.get("ADZUNA_APP_KEY", "").strip(),
        country=str(raw.get("country", "us")),
        distance_km=int(raw.get("distance_km", 40)),
        results_per_page=int(raw.get("results_per_page", 50)),
        max_days_old=int(raw.get("max_days_old", 7)),
        category=str(raw.get("category", "it-jobs")),
        what_exclude=str(raw.get("what_exclude", "")),
    )


def _read_toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _token_set(values: list[str]) -> frozenset[str]:
    return frozenset(value.strip().casefold() for value in values)
