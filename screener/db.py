"""SQLite persistence.

Plain sqlite3 rather than an ORM: the schema is one table, and a model layer
would be more code to read than it saves. Killed rows are stored, not discarded —
`stats` needs them to show what the filter did, and keeping their fingerprints is
what stops a reposted GovCon role from resurfacing next week.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from screener.filters import FilterResult
from screener.models import Job

# Where a row sits in the pipeline. `pending` means it survived the filter and is
# waiting on the screening pass, so screening can be re-run without re-pulling.
STAGE_KILLED = "killed"
STAGE_PENDING = "pending"
STAGE_SCREENED = "screened"

# Sort key for the review queue: 0 local in person, 1 remote, 2 secondary market.
# COALESCE covers rows stored before the remote check existed.
QUEUE_GROUP = (
    "CASE WHEN market = 'secondary' THEN 2 "
    "WHEN COALESCE(remote, is_remote) = 1 THEN 1 ELSE 0 END"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint     TEXT    NOT NULL UNIQUE,
    title           TEXT    NOT NULL,
    company         TEXT    NOT NULL,
    location        TEXT    NOT NULL,
    description     TEXT    NOT NULL,
    site            TEXT    NOT NULL,
    url             TEXT    NOT NULL,
    is_remote       INTEGER NOT NULL,   -- what the board claimed
    remote          INTEGER,            -- what the remote check concluded
    search_term     TEXT,               -- the searches.toml term that found it
    job_type        TEXT,
    date_posted     TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_interval TEXT,

    market          TEXT    NOT NULL,
    stage           TEXT    NOT NULL,
    killed_by       TEXT,
    flags           TEXT    NOT NULL DEFAULT '[]',   -- filter + screening, for display
    screen_flags    TEXT    NOT NULL DEFAULT '[]',   -- the screening half, so a
                                                    -- refilter cannot erase it

    verdict         TEXT,
    match_score     INTEGER,
    reasons         TEXT,
    salary_note     TEXT,

    status          TEXT    NOT NULL DEFAULT 'new',
    first_seen      TEXT    NOT NULL,
    -- Repost tracking. Dedup keeps one row per posting, so a GovCon role
    -- relisted every month has to be counted here or it leaves no trace.
    last_seen       TEXT,
    times_seen      INTEGER NOT NULL DEFAULT 1,
    screened_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_stage  ON jobs(stage);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

-- The relevance gate's answers for titles it couldn't place, keyed by the
-- normalized title, so a title is only ever sent to the model once.
CREATE TABLE IF NOT EXISTS title_classifications (
    title       TEXT    PRIMARY KEY,
    is_it       INTEGER NOT NULL,
    decided_at  TEXT    NOT NULL
);
"""


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    conn.commit()
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS never changes a table that already exists, so a
    column added after your first pull has to be added to that database here."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "remote" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN remote INTEGER")
    if "search_term" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN search_term TEXT")
    if "last_seen" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN last_seen TEXT")
    if "times_seen" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN times_seen INTEGER NOT NULL DEFAULT 1")
    if "screen_flags" not in existing:
        conn.execute("ALTER TABLE jobs ADD COLUMN screen_flags TEXT NOT NULL DEFAULT '[]'")


def known_fingerprints(conn: sqlite3.Connection) -> set[str]:
    return {row["fingerprint"] for row in conn.execute("SELECT fingerprint FROM jobs")}


def record_repeat(conn: sqlite3.Connection, fingerprint: str) -> None:
    """Count a posting the boards have listed again.

    Dedup throws the repeat away, so without this a role relisted every month
    is indistinguishable from one posted once and there is no history to read
    a repost count out of later.
    """
    conn.execute(
        "UPDATE jobs SET times_seen = times_seen + 1, last_seen = ? WHERE fingerprint = ?",
        (_now(), fingerprint),
    )
    conn.commit()


def insert_job(conn: sqlite3.Connection, job: Job, result: FilterResult, search_term: str | None = None) -> int | None:
    """Store one job with its filter outcome. Returns None if it was already known.

    Commits immediately: a pull can run for several minutes and the ingest step
    is by far the expensive part, so losing it to a crash is worse than the cost
    of a commit per row.
    """
    now = _now()
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO jobs (
            fingerprint, title, company, location, description, site, url,
            is_remote, remote, search_term, job_type, date_posted, salary_min, salary_max,
            salary_interval, market, stage, killed_by, flags, first_seen, last_seen
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            job.fingerprint,
            job.title,
            job.company,
            job.location,
            job.description,
            job.site,
            job.url,
            int(job.is_remote),
            int(result.remote),
            search_term,
            job.job_type,
            job.date_posted,
            job.salary_min,
            job.salary_max,
            job.salary_interval,
            result.market,
            STAGE_KILLED if result.killed else STAGE_PENDING,
            result.killed_by,
            json.dumps(result.flags),
            now,
            now,
        ),
    )
    conn.commit()
    return cursor.lastrowid if cursor.rowcount else None


def job_from_row(row: sqlite3.Row) -> Job:
    """Rebuild the Job a row was stored from, so the filters can run on it again."""
    return Job(
        title=row["title"],
        company=row["company"],
        location=row["location"],
        description=row["description"],
        site=row["site"],
        url=row["url"],
        is_remote=bool(row["is_remote"]),
        job_type=row["job_type"],
        date_posted=row["date_posted"],
        salary_min=row["salary_min"],
        salary_max=row["salary_max"],
        salary_interval=row["salary_interval"],
    )


def apply_filter_result(conn: sqlite3.Connection, row: sqlite3.Row, result: FilterResult) -> None:
    """Replace a stored row's filter outcome with a fresh one. Doesn't commit.

    The review mark is never touched: it records what you decided, and a rule
    change isn't a reason to undo that. Screening flags survive too: they are
    kept separately and merged back on, because re-running the filter is not a
    reason to forget that the model called a job a seniority-gap.
    """
    if result.killed:
        stage = STAGE_KILLED
    elif row["stage"] == STAGE_SCREENED:
        stage = STAGE_SCREENED
    else:
        stage = STAGE_PENDING
    from_screening = json.loads(row["screen_flags"])
    merged = list(dict.fromkeys(result.flags + from_screening))
    conn.execute(
        "UPDATE jobs SET stage = ?, killed_by = ?, market = ?, flags = ?, remote = ? WHERE id = ?",
        (stage, result.killed_by, result.market, json.dumps(merged), int(result.remote), row["id"]),
    )


def save_verdict(
    conn: sqlite3.Connection,
    job_id: int,
    verdict: str,
    match_score: int,
    reasons: list[str],
    flags: list[str],
    salary_note: str | None,
) -> None:
    """Write a screening result. Screening flags are merged with the filter's own
    rather than replacing them — both kinds matter when you scan the list."""
    existing = json.loads(conn.execute("SELECT flags FROM jobs WHERE id = ?", (job_id,)).fetchone()["flags"])
    merged = list(dict.fromkeys(existing + flags))
    conn.execute(
        """
        UPDATE jobs
           SET verdict = ?, match_score = ?, reasons = ?, flags = ?, screen_flags = ?,
               salary_note = ?, stage = ?, screened_at = ?
         WHERE id = ?
        """,
        (verdict, match_score, json.dumps(reasons), json.dumps(merged), json.dumps(flags),
         salary_note, STAGE_SCREENED, _now(), job_id),
    )
    conn.commit()


def pending_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM jobs WHERE stage = ? ORDER BY id", (STAGE_PENDING,)))


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def list_jobs(
    conn: sqlite3.Connection,
    verdicts: list[str] | None = None,
    status: str | None = None,
    flag: str | None = None,
    market: str = "primary",
    include_unscreened: bool = True,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """The review queue. Killed rows never appear here — `stats` is where you go
    to see those."""
    clauses = ["stage != ?"]
    params: list[object] = [STAGE_KILLED]

    if market != "all":
        clauses.append("market = ?")
        params.append(market)
    if verdicts:
        placeholders = ",".join("?" * len(verdicts))
        # Rows that survived the filter but haven't been screened have no verdict
        # yet. They belong in the queue by default, otherwise `pull --no-screen`
        # produces a list command that shows nothing.
        if include_unscreened:
            clauses.append(f"(verdict IN ({placeholders}) OR verdict IS NULL)")
        else:
            clauses.append(f"verdict IN ({placeholders})")
        params.extend(verdicts)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if flag:
        # Flags are a JSON array; the quotes keep "contract" from matching
        # a flag that merely contains it.
        clauses.append("flags LIKE ?")
        params.append(f'%"{flag}"%')

    params.append(limit)
    sql = (
        f"SELECT * FROM jobs WHERE {' AND '.join(clauses)} "
        # Local in-person and hybrid roles come first, then remote, then the LA
        # watchlist: you need work soon, and the employers most likely to hire you
        # are the ones you can walk into. Within each group, best score, then newest.
        f"ORDER BY {QUEUE_GROUP}, match_score IS NULL, match_score DESC, id DESC LIMIT ?"
    )
    return list(conn.execute(sql, params))


def set_status(conn: sqlite3.Connection, job_id: int, status: str) -> bool:
    cursor = conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()
    return cursor.rowcount > 0


def stage_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {row["stage"]: row["n"] for row in conn.execute("SELECT stage, COUNT(*) n FROM jobs GROUP BY stage")}


def kill_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT killed_by, COUNT(*) n FROM jobs WHERE stage = ? GROUP BY killed_by ORDER BY n DESC",
        (STAGE_KILLED,),
    )
    return [(row["killed_by"], row["n"]) for row in rows]


def verdict_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT verdict, COUNT(*) n FROM jobs WHERE verdict IS NOT NULL GROUP BY verdict ORDER BY n DESC"
    )
    return [(row["verdict"], row["n"]) for row in rows]


def status_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT status, COUNT(*) n FROM jobs WHERE stage != ? GROUP BY status ORDER BY n DESC",
        (STAGE_KILLED,),
    )
    return [(row["status"], row["n"]) for row in rows]


def market_counts(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = conn.execute(
        "SELECT market, COUNT(*) n FROM jobs WHERE stage != ? GROUP BY market ORDER BY n DESC",
        (STAGE_KILLED,),
    )
    return [(row["market"], row["n"]) for row in rows]


def get_classification(conn: sqlite3.Connection, title: str) -> bool | None:
    row = conn.execute("SELECT is_it FROM title_classifications WHERE title = ?", (title,)).fetchone()
    return None if row is None else bool(row["is_it"])


def save_classification(conn: sqlite3.Connection, title: str, is_it: bool) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO title_classifications (title, is_it, decided_at) VALUES (?, ?, ?)",
        (title, int(is_it), _now()),
    )
    conn.commit()


def killed_since(conn: sqlite3.Connection, since: str) -> list[tuple[str, str, int]]:
    """(rule, title, count) for every kill first seen at or after `since`,
    grouped so the audit reads as "this rule killed these titles"."""
    rows = conn.execute(
        """
        SELECT killed_by, title, COUNT(*) n FROM jobs
         WHERE stage = ? AND first_seen >= ?
         GROUP BY killed_by, title
         ORDER BY killed_by, n DESC, title
        """,
        (STAGE_KILLED, since),
    )
    return [(row["killed_by"], row["title"], row["n"]) for row in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
