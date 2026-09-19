"""JobSpy ingestion and normalization.

The only module that knows JobSpy exists. Everything past this point sees a list
of `Job` objects, so adding or replacing a source means changing one file.
"""

from __future__ import annotations

import math
from typing import Any

from jobspy import scrape_jobs

from screener.config import Search, SearchConfig
from screener.models import Job


def pull(search: Search, config: SearchConfig) -> list[Job]:
    """Run one search across every configured board.

    JobSpy raises on some board-side failures (rate limits, layout changes) and
    a single bad board shouldn't abandon the whole run, so failures return an
    empty list and the caller moves on to the next search.
    """
    frame = scrape_jobs(
        site_name=list(config.sites),
        search_term=search.term,
        location=search.location,
        is_remote=search.is_remote,
        results_wanted=config.results_wanted,
        hours_old=config.hours_old,
        country_indeed=config.country_indeed,
        description_format="markdown",
    )
    if frame is None or frame.empty:
        return []
    return [_to_job(row) for row in frame.to_dict("records")]


def _to_job(row: dict[str, Any]) -> Job:
    return Job(
        title=_text(row.get("title")),
        company=_text(row.get("company")),
        location=_text(row.get("location")),
        description=_text(row.get("description")),
        site=_text(row.get("site")),
        url=_text(row.get("job_url")),
        is_remote=bool(row.get("is_remote") or False),
        job_type=_text(row.get("job_type")) or None,
        date_posted=_text(row.get("date_posted")) or None,
        salary_min=_number(row.get("min_amount")),
        salary_max=_number(row.get("max_amount")),
        salary_interval=_text(row.get("interval")) or None,
    )


def _text(value: Any) -> str:
    """pandas hands back NaN for empty cells, and "nan" in a description would
    quietly become searchable text, so empties are collapsed to "" here."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number
