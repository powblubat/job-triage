"""Tests for repost counting.

A wrong count is invisible: dedup keeps one row either way, so a counter that
never increments looks exactly like a job nobody relisted.
"""

from screener import db
from screener.filters import FilterResult
from screener.models import Job


def make_job(title: str = "Help Desk Technician") -> Job:
    return Job(
        title=title,
        company="Some Nonprofit",
        location="Washington, DC, US",
        description="Support a 40-person office.",
        site="indeed",
        url="https://example.com/1",
        is_remote=False,
    )


def test_a_new_job_starts_at_one_sighting(tmp_path):
    conn = db.connect(tmp_path / "jobs.db")
    db.insert_job(conn, make_job(), FilterResult())
    row = conn.execute("SELECT times_seen, first_seen, last_seen FROM jobs").fetchone()
    assert row["times_seen"] == 1
    assert row["last_seen"] == row["first_seen"]


def test_recording_a_repeat_bumps_the_count(tmp_path):
    conn = db.connect(tmp_path / "jobs.db")
    job = make_job()
    db.insert_job(conn, job, FilterResult())
    db.record_repeat(conn, job.fingerprint)
    db.record_repeat(conn, job.fingerprint)
    assert conn.execute("SELECT times_seen FROM jobs").fetchone()["times_seen"] == 3


def test_a_relisted_posting_does_not_create_a_second_row(tmp_path):
    """The reason the count has to live on the existing row: the same posting
    under a new board id must not show up in the queue twice."""
    conn = db.connect(tmp_path / "jobs.db")
    assert db.insert_job(conn, make_job(), FilterResult()) is not None
    assert db.insert_job(conn, make_job(), FilterResult()) is None
    assert conn.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1
