"""Tests for reading the screener's reply.

A bad parse is silent in the worst direction: the job stays pending and looks
like it was never reached, so these cover the shapes models actually return.
"""

import json

from screener.screen import parse

CLEAN = '{"verdict": "pass", "score": 78, "reasons": ["a", "b"], "flags": ["contract"], "salary_note": "60k to 70k"}'


def test_a_clean_reply_parses():
    result = parse(CLEAN)
    assert result.verdict == "pass"
    assert result.score == 78
    assert result.reasons == ["a", "b"]
    assert result.salary_note == "60k to 70k"


def test_a_fenced_reply_parses():
    """Models wrap JSON in code fences no matter how the prompt is worded."""
    assert parse(f"```json\n{CLEAN}\n```").score == 78


def test_prose_around_the_json_is_ignored():
    assert parse(f"Here is my assessment:\n\n{CLEAN}\n\nHappy to revise.").score == 78


def test_an_unusable_reply_is_none():
    """None keeps the job pending to retry, rather than scoring it wrong."""
    assert parse("I cannot evaluate this posting.") is None
    assert parse('{"verdict": "strong yes", "score": 90}') is None
    assert parse('{"verdict": "pass", "score": "very high"}') is None


def test_a_score_outside_the_range_is_clamped():
    assert parse('{"verdict": "pass", "score": 140}').score == 100
    assert parse('{"verdict": "fail", "score": -20}').score == 0


def test_missing_lists_become_empty():
    result = parse('{"verdict": "fail", "score": 5}')
    assert result.reasons == [] and result.flags == [] and result.salary_note is None


def test_screening_flags_survive_a_refilter(tmp_path, config):
    """Refilter recomputes the filter's flags. It must not take the model's with
    them: tuning one rule in filters.toml would quietly strip every screening
    flag off the whole queue."""
    from screener import db
    from screener.filters import FilterResult, apply_filters
    from screener.models import Job

    job = Job(
        title="Help Desk Technician",
        company="Some Nonprofit",
        location="Washington, DC, US",
        description="Support a 40-person office.",
        site="indeed",
        url="https://example.com/1",
        is_remote=False,
    )
    conn = db.connect(tmp_path / "jobs.db")
    job_id = db.insert_job(conn, job, FilterResult(flags=["low-pay"]))
    db.save_verdict(conn, job_id, "maybe", 55, ["a reason"], ["scale-gap"], None)

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    db.apply_filter_result(conn, row, apply_filters(job, config))

    flags = json.loads(conn.execute("SELECT flags FROM jobs WHERE id = ?", (job_id,)).fetchone()["flags"])
    assert "scale-gap" in flags
