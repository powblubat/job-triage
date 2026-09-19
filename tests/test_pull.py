"""Tests for the pull loop's bookkeeping: which postings become rows, and
which rows are worth paying to score.

Both fail quietly. Copies of one job fill the review queue and read as a busy
week, and scoring jobs you already tossed costs money without saying so.
"""

from dataclasses import replace

from screener import core, db
from screener.config import Search, load_searches
from screener.filters import FilterResult
from screener.models import Job

from conftest import DEFAULTS


def remote_job(city: str, title: str = "Remote Help Desk Analyst", company: str = "Acme") -> Job:
    return Job(
        title=title,
        company=company,
        location=f"{city}, US",
        description="This role is fully remote.",
        site="indeed",
        url=f"https://example.com/{city}",
        is_remote=True,
    )


def pull(conn, config, jobs, monkeypatch):
    """One pull whose only search returns `jobs`, with no network involved."""
    searches = replace(load_searches(DEFAULTS / "searches.toml"), searches=(Search("title:(x)", "United States", True),))
    monkeypatch.setattr(core, "_sources", lambda search, cfg: [("test", lambda: jobs)])
    return core.run_pull(conn, config, searches)


def test_one_remote_posting_listed_in_many_cities_is_one_job(tmp_path, config, monkeypatch):
    conn = db.connect(tmp_path / "jobs.db")
    jobs = [remote_job(city) for city in ("Tallahassee, FL", "Boise, ID", "Denver, CO")]
    report = pull(conn, config, jobs, monkeypatch)
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1
    assert report.kept == 1
    assert report.duplicates == 2


def test_a_later_pull_does_not_add_another_city(tmp_path, config, monkeypatch):
    conn = db.connect(tmp_path / "jobs.db")
    pull(conn, config, [remote_job("Tallahassee, FL")], monkeypatch)
    pull(conn, config, [remote_job("Boise, ID")], monkeypatch)
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1


def test_different_remote_jobs_at_one_company_are_kept(tmp_path, config, monkeypatch):
    conn = db.connect(tmp_path / "jobs.db")
    jobs = [remote_job("Denver, CO"), remote_job("Boise, ID", title="Remote Desktop Support Technician")]
    pull(conn, config, jobs, monkeypatch)
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 2


def test_an_on_site_chain_is_not_collapsed(tmp_path, config, monkeypatch):
    """The same title at one employer in two cities, on site, is two jobs."""
    conn = db.connect(tmp_path / "jobs.db")
    jobs = [
        replace(remote_job(city), is_remote=False, description="On site support for the branch.")
        for city in ("Arlington, VA", "Rockville, MD")
    ]
    pull(conn, config, jobs, monkeypatch)
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 2


def test_only_undecided_and_saved_jobs_wait_for_a_score(tmp_path):
    conn = db.connect(tmp_path / "jobs.db")
    ids = {}
    for status in ("new", "saved", "ignored", "applied", "rejected", "expired"):
        ids[status] = db.insert_job(conn, remote_job(status), FilterResult(remote=False))
        db.set_status(conn, ids[status], status)
    pending = {row["id"] for row in db.pending_jobs(conn)}
    assert pending == {ids["new"], ids["saved"]}
