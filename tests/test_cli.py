"""Tests for the command-line argument checks: the ones that guard money, and
the ones that keep a typed mark from quietly matching nothing."""

import pytest
from typer.testing import CliRunner

from screener import db, setup
from screener.cli import app
from screener.filters import FilterResult
from screener.models import Job


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_screen_refuses_a_limit_below_one(limit):
    """A negative limit slices from the end of the queue, so --limit -1 would
    screen every pending job but one: the opposite of pricing a small run.
    Refused by the option itself, before the command body runs."""
    result = CliRunner().invoke(app, ["screen", "--limit", limit])
    assert result.exit_code == 2


@pytest.fixture
def saved_job(tmp_path, monkeypatch):
    """A home folder holding one job, already saved: the case where a posting
    got past the filters, looked good, and turned out not to be."""
    monkeypatch.setenv("SCREENER_HOME", str(tmp_path))
    setup.init(tmp_path)
    conn = db.connect(tmp_path / "jobs.db")
    job = Job(title="IT Specialist", company="Somewhere", location="Arlington, VA, US",
              description="Five years required.", site="indeed", url="https://example.com/1",
              is_remote=False)
    job_id = db.insert_job(conn, job, FilterResult())
    db.set_status(conn, job_id, "saved")
    return conn, job_id


def test_a_saved_job_can_be_tossed(saved_job):
    conn, job_id = saved_job
    result = CliRunner().invoke(app, ["mark", str(job_id), "tossed"])
    assert result.exit_code == 0
    assert "tossed" in result.output
    # Stored as the same mark review's `t` key writes, so the two agree.
    assert db.get_job(conn, job_id)["status"] == "ignored"


def test_a_mistyped_mark_is_refused_not_ignored(saved_job):
    conn, job_id = saved_job
    result = CliRunner().invoke(app, ["mark", str(job_id), "tosed"])
    assert result.exit_code == 1
    assert db.get_job(conn, job_id)["status"] == "saved"
    assert CliRunner().invoke(app, ["list", "--marked", "tosed"]).exit_code == 1
