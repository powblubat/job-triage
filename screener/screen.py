"""The fit judgment: how well does one posting match the candidate?

The one place that spends real money per job, so it runs last, only on postings
that survived the gate and every mechanical rule. The facts it is allowed to
claim come from `profile/facts.md` rather than from this file, so the screener
and the cover letter writer can never disagree about the background.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from screener import db, llm
from screener.models import Job
from screener.setup import FACTS_FILE, default_text

# The template `screener init` writes as facts.md, to be filled in place.
EXAMPLE_FILE = "facts.example.md"

# Long postings are mostly boilerplate: benefits, EEO statements, "about us".
# The requirements are near the top, and this keeps a 20,000-character posting
# from costing ten times what a normal one does.
DESCRIPTION_CHARS = 6000
# Four capped reasons plus flags runs to about 550 tokens. The headroom is for
# the postings that earn four long ones: a truncated reply fails to parse and
# the job gets screened, and paid for, twice.
MAX_TOKENS = 1500

PASS, MAYBE, FAIL = "pass", "maybe", "fail"

FRAMING = """You screen job postings for one candidate, and you are deliberately hard to \
impress. A job seeker's own judgment turns generous when the search drags on, so \
a screener that inflates fit is worse than no screener at all. Score the mismatch \
honestly and let the candidate argue with you.

These are the only claims about the candidate you may treat as true:

{facts}

Anything not stated above, the candidate does not have. Where the facts say \
what the background is not, hold to it: do not round it up. Do not infer \
experience from a job title and do not give credit for being adjacent to it.

Score fit from 0 to 100 and return one verdict:

  pass   70 and up, worth the time it takes to apply
  maybe  40 to 69, real arguments both ways
  fail   under 40, a clear mismatch

Hard rules:

- If the role is not IT or computer support work, return fail with a score of 10 \
or less and the flag "not-it". The title filter ahead of you is not perfect.
- Seniority the facts do not support is a mismatch, not a stretch. Team lead, \
architect, or a firm requirement of five or more years is a fail.
- A requirement the candidate does not meet lowers the score. Do not explain it away.
- Judge the posting in front of you. Do not assume the employer will be flexible.
- Keep every reason under 20 words. A reply that runs long gets cut off and \nthe call is wasted.

Reply with JSON only, no prose around it, in exactly this shape:

{{"verdict": "pass|maybe|fail",
 "score": 0-100,
 "reasons": ["two to four, each under 20 words, concrete and tied to this posting"],
 "flags": ["kebab-case", "at most four"],
 "salary_note": "pay as the posting states it, or null"}}

Useful flags: not-it, seniority-gap, degree-required, clearance-risk, scale-gap, \
network-design, travel, on-call, cert-gap. Add others in the same style when \
they matter."""

POSTING = """Title: {title}
Company: {company}
Location: {location}
Pay from the board: {salary}

{description}"""


@dataclass(frozen=True)
class Verdict:
    verdict: str
    score: int
    reasons: list[str]
    flags: list[str]
    salary_note: str | None


@dataclass
class ScreenReport:
    screened: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    verdicts: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.verdicts is None:
            self.verdicts = {PASS: 0, MAYBE: 0, FAIL: 0}


class ProfileNotFound(Exception):
    """Raised rather than screening against an empty profile: a model given no
    background scores everything a pass, which is the failure you would notice
    last."""


def load_facts(root: Path) -> str:
    path = root / FACTS_FILE
    facts = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    if not facts:
        raise ProfileNotFound(f'{path} is missing or empty; run "screener init" and fill it in')
    # init writes the template itself as facts.md. Screening against it is the
    # same failure as an empty profile, only quieter.
    if _same_text(facts, default_text(EXAMPLE_FILE)):
        raise ProfileNotFound(f"{path} is still the template; fill it in with your own background")
    return facts


def _same_text(a: str, b: str) -> bool:
    return a.replace("\r\n", "\n").strip() == b.replace("\r\n", "\n").strip()


def screen_job(client: llm.Client, job: Job, facts: str) -> tuple[Verdict | None, llm.Reply | None]:
    """The verdict for one posting, and the reply it came from so the caller can
    add up what the run cost. A None verdict means the call or the parse failed
    and the job keeps its pending stage, to be retried rather than scored wrong."""
    prompt = POSTING.format(
        title=job.title,
        company=job.company or "unknown",
        location=job.location or "unknown",
        salary=_salary(job),
        description=job.description[:DESCRIPTION_CHARS],
    )
    reply = llm.ask(client, llm.SCREEN_MODEL, FRAMING.format(facts=facts), prompt, MAX_TOKENS)
    if reply is None:
        return None, None
    return parse(reply.text), reply


def parse(text: str) -> Verdict | None:
    """Pull the verdict out of a reply. Models wrap JSON in prose or code fences
    often enough that finding the object is worth the few lines."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None

    verdict = str(data.get("verdict", "")).strip().lower()
    if verdict not in (PASS, MAYBE, FAIL):
        return None
    try:
        score = int(data.get("score"))
    except (TypeError, ValueError):
        return None

    salary_note = data.get("salary_note")
    return Verdict(
        verdict=verdict,
        score=max(0, min(100, score)),
        reasons=[str(r) for r in data.get("reasons") or []][:4],
        flags=[str(f) for f in data.get("flags") or []][:4],
        salary_note=str(salary_note) if salary_note else None,
    )


def screen_pending(
    conn: sqlite3.Connection,
    client: llm.Client,
    facts: str,
    limit: int | None = None,
    progress=lambda _: None,
) -> ScreenReport:
    report = ScreenReport()
    rows = db.pending_jobs(conn)
    if limit is not None:
        rows = rows[:limit]

    for row in rows:
        verdict, reply = screen_job(client, db.job_from_row(row), facts)
        if reply is not None:
            report.prompt_tokens += reply.prompt_tokens
            report.completion_tokens += reply.completion_tokens
            report.cost += reply.cost
        if verdict is None:
            report.failed += 1
            progress(f"  #{row['id']} screening failed, left pending")
            continue

        db.save_verdict(
            conn, row["id"], verdict.verdict, verdict.score,
            verdict.reasons, verdict.flags, verdict.salary_note,
        )
        report.screened += 1
        report.verdicts[verdict.verdict] += 1
        progress(f"  #{row['id']:<5} {verdict.verdict:<5} {verdict.score:>3}  {row['title'][:48]}")

    return report


def _salary(job: Job) -> str:
    if not job.salary_min and not job.salary_max:
        return "not stated"
    low = f"{job.salary_min:,.0f}" if job.salary_min else "?"
    high = f"{job.salary_max:,.0f}" if job.salary_max else "?"
    return f"{low} to {high} per {job.salary_interval or 'unknown period'}"
