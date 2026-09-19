"""Tests for fitting one job onto one screen.

This failure is silent in the worst direction. When the card is taller than the
window the terminal scrolls, and the lines that go are the first ones printed:
the title, the company and the location. You end up deciding on a posting you
can no longer identify.
"""

import os
import shutil

from screener import db, display, triage
from screener.filters import FilterResult
from screener.models import Job

# Long enough to wrap even in a wide window, which is what the real ones do and
# what made the header overflow in the first place.
LONG_REASON = (
    "Requires four to seven years of SharePoint and M365 specialist experience, well beyond "
    "the one support role in a thirty person office that the profile actually describes"
)


def screened_row(tmp_path):
    conn = db.connect(tmp_path / "jobs.db")
    job = Job(
        title="Deskside Specialist",
        company="Metropolitan Washington Airports Authority",
        location="Dulles, VA, US",
        description="QUALIFICATIONS\n" + "Three years of progressively responsible experience.\n" * 40,
        site="indeed",
        url="https://www.indeed.com/viewjob?jk=a8ad32aa9db571dc",
        is_remote=False,
    )
    job_id = db.insert_job(conn, job, FilterResult(flags=["shift-risk"]))
    db.save_verdict(
        conn, job_id, "maybe", 45, [LONG_REASON] * 4,
        ["scale-gap", "cert-gap", "on-call", "travel"], "32 to 45 per hour",
    )
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def drawn_rows(row, height, monkeypatch):
    """The rows `screener review` actually prints for one job at this window size."""
    monkeypatch.setattr(shutil, "get_terminal_size", lambda: os.terminal_size((100, height)))
    counter = "[1/34]  #1"
    width, max_rows, max_reasons = triage._fit_to_window(row, counter, False)
    card = display.card(row, max_rows=max_rows, width=width, max_reasons=max_reasons)
    lines = [counter, *card.split("\n"), triage.MENU]
    return card, display.screen_rows(lines, width) + 1  # the row the cursor lands on


def test_the_card_never_outgrows_the_window(tmp_path, monkeypatch):
    row = screened_row(tmp_path)
    for height in (24, 30, 40, 60):
        _, used = drawn_rows(row, height, monkeypatch)
        assert used <= height, f"{used} rows drawn in a {height}-row window"


def test_the_employer_survives_the_smallest_window(tmp_path, monkeypatch):
    """A verdict and four wrapped reasons used to take the whole header past
    twenty rows. The reasons are what gets dropped, not the identity."""
    row = screened_row(tmp_path)
    card, _ = drawn_rows(row, 24, monkeypatch)
    assert "Deskside Specialist" in card
    assert "Metropolitan Washington Airports Authority" in card
    assert "Dulles, VA, US" in card


def test_trimmed_reasons_say_where_to_read_them(tmp_path, monkeypatch):
    """A short excerpt already says how much it left out; a short reason list has
    to do the same, or it passes for the model's whole argument."""
    row = screened_row(tmp_path)
    card, _ = drawn_rows(row, 24, monkeypatch)
    assert "more - screener show" in card


def test_a_tall_window_shows_every_reason(tmp_path, monkeypatch):
    row = screened_row(tmp_path)
    card, _ = drawn_rows(row, 60, monkeypatch)
    # Counted by bullet, not by whole string: a reason long enough to wrap is
    # spread over several rows, and the continuation rows are indented further.
    assert len([line for line in card.split("\n") if line.startswith("  - ")]) == 4
    assert "more - screener show" not in card


def test_reasons_wrap_to_the_card_width(tmp_path, monkeypatch):
    """An unwrapped reason runs past the rule and wraps at the terminal edge,
    which drops the bullet's indent and makes four reasons look like eight."""
    row = screened_row(tmp_path)
    card, _ = drawn_rows(row, 60, monkeypatch)
    width = display.terminal_size()[0]
    assert max(len(line) for line in card.split("\n")) <= width
