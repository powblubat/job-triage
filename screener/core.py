"""The pipeline, in one direction: pull -> normalize -> dedup -> gate -> filter -> store.

The CLI is a thin wrapper over these functions and holds no logic of its own, so
a different front end later reuses this file unchanged rather than reimplementing
the pipeline against the database.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from screener import adzuna, classify, db, display, filters, gate, ingest, llm
from screener.config import FilterConfig, Search, SearchConfig
from screener.filters import SECONDARY, FilterResult
from screener.models import Job

Progress = Callable[[str], None]


@dataclass
class PullReport:
    pulled: int = 0
    duplicates: int = 0
    reposts: int = 0
    killed: Counter = field(default_factory=Counter)
    kept_primary: int = 0
    kept_secondary: int = 0
    failed_searches: list[str] = field(default_factory=list)
    # What the relevance gate did, so the pull can say how much it decided
    # itself and how much it had to ask about.
    gate_allowed: int = 0
    gate_denied: int = 0
    gate_ambiguous: int = 0
    classified_it: int = 0
    classified_not: int = 0
    classify_unanswered: int = 0

    @property
    def kept(self) -> int:
        return self.kept_primary + self.kept_secondary

    @property
    def killed_total(self) -> int:
        return sum(self.killed.values())


@dataclass
class MarkedChange:
    """A job you had already reviewed whose filter outcome a rule change moved."""

    job_id: int
    status: str
    title: str
    location: str
    before: str
    after: str


@dataclass
class RefilterReport:
    checked: int = 0
    killed: Counter = field(default_factory=Counter)
    revived: int = 0
    remote_removed: int = 0
    remote_added: int = 0
    flags_added: Counter = field(default_factory=Counter)
    flags_removed: Counter = field(default_factory=Counter)
    marked_changes: list[MarkedChange] = field(default_factory=list)
    # False when the gate wanted to classify and had no key to do it with.
    classify_available: bool = True


def evaluate(job: Job, filter_config: FilterConfig, classifier: classify.Classifier, report: PullReport) -> FilterResult:
    """Gate, then kill rules, then the paid question for what's still standing.

    The gate's deterministic part runs first so a forklift job is counted as
    "not IT", not "wrong location". The model is only asked about a job that
    survived everything else: no point paying to categorize a cleared posting.
    """
    verdict = gate.check(job.title, filter_config.gate)
    if verdict.denied:
        report.gate_denied += 1
        return FilterResult(killed_by=f"gate:{verdict.rule}")

    result = filters.apply_filters(job, filter_config)
    if not verdict.ambiguous:
        report.gate_allowed += 1
        return result

    report.gate_ambiguous += 1
    if result.killed:
        return result

    mode = filter_config.gate.ambiguous
    if mode == "kill":
        return FilterResult(killed_by="gate:ambiguous", remote=result.remote)
    if mode == "classify":
        answer = classifier.is_it_role(job.title, job.description)
        if answer is False:
            report.classified_not += 1
            return FilterResult(killed_by="gate:classify", remote=result.remote)
        if answer is True:
            report.classified_it += 1
        else:
            report.classify_unanswered += 1
    return result


def run_pull(
    conn: sqlite3.Connection,
    filter_config: FilterConfig,
    search_config: SearchConfig,
    progress: Progress = lambda _: None,
) -> PullReport:
    report = PullReport()
    classifier = classify.Classifier(conn, llm.client_from_env())

    # Two sets, because the two kinds of repeat mean different things. A
    # fingerprint already stored means the boards listed this posting again,
    # which is the repost signal. A fingerprint first seen during this run is
    # just a second search term finding the same job, and counts for nothing.
    stored = db.known_fingerprints(conn)
    seen_this_run: set[str] = set()

    if search_config.adzuna.enabled and not search_config.adzuna.configured:
        progress("adzuna   skipped: ADZUNA_APP_ID or ADZUNA_APP_KEY missing from .env")
    if filter_config.gate.ambiguous == "classify" and not classifier.available:
        progress("classify skipped: OPENROUTER_API_KEY missing from .env; ambiguous titles are kept")

    for search in search_config.searches:
        label = f"{search.term} @ {search.location}" + (" (remote)" if search.is_remote else "")
        for source, pull in _sources(search, search_config):
            progress(f"pulling  {label} via {source}")
            try:
                jobs = pull()
            except Exception as error:  # noqa: BLE001 - one bad source must not end the run
                progress(f"  failed: {error}")
                report.failed_searches.append(f"{label} via {source}: {error}")
                continue

            for job in jobs:
                report.pulled += 1
                if job.fingerprint in seen_this_run:
                    report.duplicates += 1
                    continue
                seen_this_run.add(job.fingerprint)

                if job.fingerprint in stored:
                    report.duplicates += 1
                    report.reposts += 1
                    db.record_repeat(conn, job.fingerprint)
                    continue

                result = evaluate(job, filter_config, classifier, report)
                db.insert_job(conn, job, result, search.term)

                if result.killed:
                    report.killed[result.killed_by] += 1
                elif result.market == SECONDARY:
                    report.kept_secondary += 1
                else:
                    report.kept_primary += 1

            progress(f"  {len(jobs)} results")

    return report


def _sources(search: Search, search_config: SearchConfig) -> list[tuple[str, Callable[[], list]]]:
    """The pulls to run for one search, as (name, thunk) pairs.

    The JobSpy boards always. Adzuna only for local searches: it has no remote
    flag, and the remote check can't work on the 500-character snippet it returns.
    """
    sources: list[tuple[str, Callable[[], list]]] = [
        (", ".join(search_config.sites), lambda: ingest.pull(search, search_config)),
    ]
    adzuna_config = search_config.adzuna
    if adzuna_config.enabled and adzuna_config.configured and not search.is_remote:
        sources.append(
            ("adzuna", lambda: adzuna.pull(search, adzuna_config, search_config.results_wanted)),
        )
    return sources


def refilter(conn: sqlite3.Connection, filter_config: FilterConfig, dry_run: bool = False) -> RefilterReport:
    """Run the current filters.toml over every stored job.

    `pull` only filters postings it hasn't seen before, so without this a rule
    change reaches next week's jobs but not the queue you're working through now.
    Marks are left alone: a marked job a new rule kills just stops appearing in
    review, list and export, and `show` still opens it.
    """
    report = RefilterReport()
    # The gate runs here too, with the same cache, so a stored title that was
    # already classified costs nothing and a new deny pattern reaches old rows.
    classifier = classify.Classifier(conn, llm.client_from_env())
    report.classify_available = classifier.available or filter_config.gate.ambiguous != "classify"
    for row in conn.execute("SELECT * FROM jobs ORDER BY id").fetchall():
        result = evaluate(db.job_from_row(row), filter_config, classifier, PullReport())
        report.checked += 1

        was_killed = row["stage"] == db.STAGE_KILLED
        if result.killed and not was_killed:
            report.killed[result.killed_by] += 1
        elif was_killed and not result.killed:
            report.revived += 1

        # Labels and flags are only counted on jobs you could see before and after.
        if not was_killed and not result.killed:
            was_remote = display.is_remote(row)
            if was_remote and not result.remote:
                report.remote_removed += 1
            elif result.remote and not was_remote:
                report.remote_added += 1
            # Compared against what will actually be written, screening flags
            # included. Comparing against the filter alone reported every
            # screening flag as removed on each run, which was never true.
            old_flags = set(json.loads(row["flags"]))
            new_flags = set(result.flags) | set(json.loads(row["screen_flags"]))
            report.flags_added.update(new_flags - old_flags)
            report.flags_removed.update(old_flags - new_flags)

        before = row["killed_by"] or row["market"]
        after = result.killed_by or result.market
        if row["status"] != "new" and before != after:
            report.marked_changes.append(
                MarkedChange(row["id"], row["status"], row["title"], row["location"], before, after)
            )

        if not dry_run:
            db.apply_filter_result(conn, row, result)

    if not dry_run:
        # One commit for the whole pass, so a crash halfway can't leave half the
        # queue under the old rules and half under the new ones.
        conn.commit()
    return report


def load_configs(root: Path) -> tuple[FilterConfig, SearchConfig]:
    from screener import config

    # Keys live next to the config files, never in them.
    load_dotenv(root / ".env")
    searches = config.load_searches(root / config.SEARCHES_FILE)
    return config.load_filters(root / config.FILTERS_FILE, searches), searches
