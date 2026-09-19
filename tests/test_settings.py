"""Tests for the title, location and govcon settings, and the config they feed.

Two silent failures live here. A CLI edit that loses the comments in a TOML
file throws away the reasons behind every rule. A location or govcon change
that doesn't reach the filter means jobs die, or survive, without a sound.
"""

import shutil
from pathlib import Path

import pytest

from screener import settings
from screener.config import build_searches, build_term, load_filters, load_searches, Location
from screener.filters import apply_filters
from screener.gate import ALLOW, AMBIGUOUS, check
from screener.models import Job

ROOT = Path(__file__).parent.parent


@pytest.fixture
def home(tmp_path):
    """Copies of the real config files, so the CLI's edits can be tested on
    exactly the files a user has."""
    for name in ("filters.toml", "searches.toml"):
        shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


def load(home: Path):
    searches = load_searches(home / "searches.toml")
    return load_filters(home / "filters.toml", searches), searches


def make_job(**overrides) -> Job:
    defaults = dict(
        title="IT Support Specialist",
        company="Some Nonprofit",
        location="Washington, DC, US",
        description="Support a 40-person office. Entra ID, Intune, ticketing.",
        site="indeed",
        url="https://example.com/1",
        is_remote=False,
    )
    return Job(**{**defaults, **overrides})


# --- building the queries ---


def test_only_multi_word_titles_are_quoted():
    assert build_term(("help desk", "helpdesk")) == 'title:("help desk" OR helpdesk)'


def test_titles_are_chunked_and_run_in_every_location():
    places = (Location("Washington, DC"), Location("United States", remote=True))
    searches = build_searches(("a", "b", "c"), places, per_search=2)
    assert [(s.term, s.location, s.is_remote) for s in searches] == [
        ("title:(a OR b)", "Washington, DC", False),
        ("title:(c)", "Washington, DC", False),
        ("title:(a OR b)", "United States", True),
        ("title:(c)", "United States", True),
    ]


# --- titles ---


def test_adding_and_removing_a_title_leaves_the_file_as_it_was(home):
    path = home / "searches.toml"
    before = path.read_bytes()
    added, present = settings.add_titles(path, ["network administrator", "HELP  desk"])
    assert added == ["network administrator"]
    assert present == ["HELP desk"]
    assert "network administrator" in load_searches(path).titles

    removed, missing = settings.remove_titles(path, ["Network Administrator", "nope"])
    assert removed == ["network administrator"]
    assert missing == ["nope"]
    assert path.read_bytes() == before


def test_a_searched_title_is_let_through_the_gate(home):
    """A title you chose to search for must not then die as "not an IT job"."""
    config, _ = load(home)
    assert check("Imaging Coordinator", config.gate).verdict == AMBIGUOUS

    settings.add_titles(home / "searches.toml", ["imaging coordinator"])
    config, _ = load(home)
    assert check("Imaging Coordinator", config.gate).verdict == ALLOW


def test_a_title_the_filters_would_kill_is_reported(config):
    assert settings.title_conflicts("Senior Help Desk Analyst", config) == ['title_kill "senior"']
    assert settings.title_conflicts("Help Desk Analyst", config) == []


# --- locations ---


def test_a_new_location_keeps_its_state_by_default(home):
    config, _ = load(home)
    austin = make_job(location="Round Rock, TX, US")
    assert apply_filters(austin, config).killed_by == "location"

    place = settings.add_location(home / "searches.toml", "Austin, TX")
    assert place["match"] == ["tx"]
    config, searches = load(home)
    assert not apply_filters(austin, config).killed
    assert any(search.location == "Austin, TX" for search in searches.searches)


def test_an_explicit_match_narrows_a_location(home):
    settings.add_location(home / "searches.toml", "Austin, TX", match=["Austin"])
    config, _ = load(home)
    assert not apply_filters(make_job(location="Austin, TX, US"), config).killed
    assert apply_filters(make_job(location="Houston, TX, US"), config).killed_by == "location"


def test_a_secondary_location_is_tagged(home):
    settings.add_location(home / "searches.toml", "San Diego, CA", match=["san diego"], secondary=True)
    config, _ = load(home)
    result = apply_filters(make_job(location="San Diego, CA, US"), config)
    assert result.market == "secondary"
    assert "secondary-market" in result.flags


def test_a_duplicate_location_is_refused(home):
    with pytest.raises(settings.SettingsError):
        settings.add_location(home / "searches.toml", "washington, dc")


def test_adding_and_removing_a_location_leaves_the_file_as_it_was(home):
    path = home / "searches.toml"
    before = path.read_bytes()
    settings.add_location(path, "Austin, TX")
    settings.remove_location(path, "AUSTIN, TX")
    assert path.read_bytes() == before


def test_removing_a_location_stops_keeping_its_jobs(home):
    settings.remove_location(home / "searches.toml", "Washington, DC")
    config, _ = load(home)
    assert apply_filters(make_job(), config).killed_by == "location"


def test_edits_keep_windows_line_endings(home):
    """A CRLF file must stay all CRLF: tomlkit writes its own lines with "\\n",
    and mixed endings turn a one-line edit into a whole-file diff."""
    path = home / "searches.toml"
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    settings.add_titles(path, ["network administrator"])
    settings.add_location(path, "Austin, TX")
    data = path.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n")


# --- govcon ---


def test_govcon_off_lets_cleared_and_contractor_jobs_through(home):
    cleared = make_job(description="Active TS/SCI clearance required.")
    contractor = make_job(company="Leidos")
    config, _ = load(home)
    assert config.govcon_enabled
    assert apply_filters(cleared, config).killed
    assert apply_filters(contractor, config).killed_by == "company:Leidos"

    assert settings.set_govcon(home / "filters.toml", False) is True
    config, _ = load(home)
    assert not config.govcon_enabled
    assert not apply_filters(cleared, config).killed
    assert not apply_filters(contractor, config).killed


def test_govcon_off_keeps_the_general_blocklist(home):
    settings.set_govcon(home / "filters.toml", False)
    config, _ = load(home)
    assert apply_filters(make_job(company="Planet Fitness"), config).killed_by == "company:Planet Fitness"


def test_toggling_govcon_leaves_the_file_as_it_was(home):
    path = home / "filters.toml"
    before = path.read_bytes()
    settings.set_govcon(path, False)
    settings.set_govcon(path, True)
    assert path.read_bytes() == before
