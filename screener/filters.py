"""The mechanical kill/flag pass. Nothing in this module calls an API.

This is where most rows die, which makes it the one place a silent bug is
genuinely expensive: a wrong rule here doesn't raise, it just means a job you
wanted never reaches your eyes. That is why this module has tests and the rest
of the codebase mostly doesn't.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

from screener import text
from screener.config import FilterConfig, RemoteRules
from screener.models import Job

PRIMARY = "primary"
SECONDARY = "secondary"

CONTRACT_FLAG = "contract"
LOW_PAY_FLAG = "low-pay"

# Killed by the location rule only because the remote check said no. Named apart
# from plain "location" so `screener stats` shows how much the remote check catches.
NOT_REMOTE_KILL = "location:not-remote"

# How JobSpy's `interval` values convert to a yearly figure. 2080 = 40h x 52wk.
_PERIODS_PER_YEAR = {
    "yearly": 1,
    "annual": 1,
    "monthly": 12,
    "weekly": 52,
    "daily": 260,
    "hourly": 2080,
}

_WHITESPACE = re.compile(r"\s+")
_NEVER_MATCHES = re.compile(r"(?!)")


@dataclass
class FilterResult:
    killed_by: str | None = None
    market: str = PRIMARY
    flags: list[str] = field(default_factory=list)
    # Whether the job really is remote, as opposed to what the board claimed.
    remote: bool = False

    @property
    def killed(self) -> bool:
        return self.killed_by is not None


def apply_filters(job: Job, config: FilterConfig) -> FilterResult:
    """Run every mechanical rule against one job.

    Rule order decides which rule gets the credit in `screener stats` when more
    than one would have killed the row. Employer and clearance run before
    location on purpose: those are the GovCon noise you're actually trying to
    measure, and counting a cleared Texas role as "out of area" would hide it.
    """
    # Phrases are matched against a normalized copy. JobSpy stores descriptions
    # with markdown escapes ("12\-hour"), and matching the raw text let hyphenated
    # phrases miss without any error.
    haystack = text.for_matching(f"{job.title}\n{job.description}")
    remote = _really_remote(job, config.remote)

    blocked = _blocked_company(job.company, config.blocked_companies)
    if blocked:
        return FilterResult(killed_by=f"company:{blocked}", remote=remote)

    keyword = _first_unnegated(haystack, config.kill_keywords, config)
    if keyword:
        return FilterResult(killed_by=f"keyword:{keyword}", remote=remote)

    title_word = _title_kill(job.title, config.title_kill)
    if title_word:
        return FilterResult(killed_by=f"title:{title_word}", remote=remote)

    market = _classify_market(job, remote, config)
    if market is None:
        killed_by = NOT_REMOTE_KILL if job.is_remote and not remote else "location"
        return FilterResult(killed_by=killed_by, remote=remote)

    flags = _soft_flags(job, haystack, config)
    if market == SECONDARY:
        flags.insert(0, config.secondary_tag)

    return FilterResult(market=market, flags=flags, remote=remote)


def _blocked_company(company: str, blocklist: tuple[str, ...]) -> str | None:
    """Word-boundary match, so "Booz Allen" catches "Booz Allen Hamilton" but
    "Jacobs" does not catch "Jacobsen Consulting"."""
    if not company:
        return None
    for blocked in blocklist:
        if _boundary_pattern((blocked,)).search(company):
            return blocked
    return None


def _title_kill(title: str, words: tuple[str, ...]) -> str | None:
    """The first title word that marks the role as not yours: seniority,
    management, or a specialty the wider searches drag in. Title only, and no
    negation window — "not a senior role" isn't a phrase titles use."""
    normalized = text.for_matching(title)
    for word in words:
        if matching_pattern((word,), plural=True).search(normalized):
            return word
    return None


def _first_unnegated(haystack: str, keywords: tuple[str, ...], config: FilterConfig) -> str | None:
    for keyword in keywords:
        if _keyword_hits(haystack, keyword, config):
            return keyword
    return None


def _keyword_hits(haystack: str, keyword: str, config: FilterConfig) -> bool:
    """True when `keyword` appears somewhere that isn't negated.

    DC-area postings are full of "no security clearance required" and "clearance
    is not required" — a plain substring match would delete exactly the listings
    worth reading. So each occurrence is checked against a window of text on
    either side, and only an occurrence with no negation cue nearby counts.
    """
    before_cues = matching_pattern(config.negation_before)
    after_cues = matching_pattern(config.negation_after)
    window = config.window_chars

    for match in matching_pattern((keyword,)).finditer(haystack):
        before = haystack[max(0, match.start() - window) : match.start()]
        after = haystack[match.end() : match.end() + window]
        if before_cues.search(before) or after_cues.search(after):
            continue
        return True
    return False


def _really_remote(job: Job, rules: RemoteRules) -> bool:
    """Whether a job the board calls remote actually is.

    LinkedIn and Indeed both tag a job remote when the word appears anywhere in
    it, so "provide onsite and remote support" is enough — 232 of the first 368
    survivors carried the tag. This asks the posting instead. First match wins:

    1. On-site or hybrid wording: not remote, even if it also says remote
       somewhere ("this is not a remote role ... our remote-first team").
    2. Remote stated outright, or "remote" in the title or location: remote.
    3. Loose wording ("may be remote, hybrid, or onsite"): remote. Keeping these
       is the safe direction; you read one and toss it.
    4. No mention of remote anywhere: remote. The tag can't have come from a
       stray word, so it came from the board's own listing data.
    5. Anything else: not remote.
    """
    if not job.is_remote:
        return False

    title = text.for_matching(job.title)
    description = text.for_matching(job.description)
    location = text.for_matching(job.location)

    if _phrase_in(description, rules.contradicted_by) or _phrase_in(title, rules.title_contradicted_by):
        return False
    if _stated(description, rules.stated_by, rules) or _phrase_in(f"{title} | {location}", ("remote",)):
        return True
    if _stated(description, rules.loosely_stated_by, rules):
        return True
    everything = f"{title} {description} {location}"
    return not any(word in everything for word in ("remote", "wfh", "work from home"))


def _stated(description: str, phrases: tuple[str, ...], rules: RemoteRules) -> bool:
    """A remote phrase that isn't negated just before it. "This role can not be
    done fully remote" contains "fully remote", and read as a plain phrase match
    it made a Boston office job remote."""
    cues = matching_pattern(rules.negated_by)
    for match in matching_pattern(phrases).finditer(description):
        before = description[max(0, match.start() - rules.negation_window) : match.start()]
        if not cues.search(before):
            return True
    return False


def _classify_market(job: Job, remote: bool, config: FilterConfig) -> str | None:
    """"primary", "secondary", or None meaning kill."""
    tokens = _location_tokens(job.location)

    # Remote is checked first because boards routinely stamp a remote posting
    # with the employer's HQ, which would otherwise fail the DC/MD/VA test.
    if config.keep_remote_us and remote and not (tokens & config.foreign_markers):
        return PRIMARY

    if tokens & config.primary_locations:
        return PRIMARY
    if tokens & config.secondary_locations:
        return SECONDARY
    return None


def _location_tokens(location: str) -> set[str]:
    """JobSpy formats locations as "City, ST, Country". Splitting on commas gives
    clean whole tokens, which stops "VA" matching inside an unrelated word."""
    if not location:
        return set()
    parts = (_WHITESPACE.sub(" ", part).strip().casefold() for part in location.split(","))
    return {part for part in parts if part}


def _soft_flags(job: Job, haystack: str, config: FilterConfig) -> list[str]:
    flags: list[str] = []

    for rule in config.keyword_flags:
        if _first_unnegated(haystack, rule.keywords, config):
            flags.append(rule.flag)

    if job.job_type and job.job_type.strip().casefold() in config.contract_job_types:
        flags.append(CONTRACT_FLAG)

    salary = _annual_salary(job)
    if salary is not None and salary < config.low_pay_below:
        flags.append(LOW_PAY_FLAG)

    # A job can earn "contract" from both its job_type and its wording.
    return list(dict.fromkeys(flags))


def _annual_salary(job: Job) -> float | None:
    """Lowest advertised figure, annualized.

    None when the posting has no usable salary — most of them don't, and a
    missing number must not read as a low number or half the queue gets a
    low-pay flag it didn't earn.
    """
    amount = job.salary_min if job.salary_min else job.salary_max
    if not amount or not job.salary_interval:
        return None
    multiplier = _PERIODS_PER_YEAR.get(job.salary_interval.strip().casefold())
    if multiplier is None:
        return None
    return float(amount) * multiplier


def _phrase_in(haystack: str, phrases: tuple[str, ...]) -> bool:
    return bool(matching_pattern(phrases).search(haystack))


@lru_cache(maxsize=None)
def matching_pattern(phrases: tuple[str, ...], plural: bool = False) -> re.Pattern[str]:
    """A boundary pattern for text that has been through `text.for_matching`.
    Public because the relevance gate matches titles the same way.

    `plural` also accepts a trailing "s". Titles need it: Indeed writes some as
    "Electronics Technicians", and the singular phrase in filters.toml has to
    catch them. Descriptions must not have it, because "clearances" in HR
    boilerplate means a background check — pluralizing the clearance keywords
    killed an IT Support role whose only sin was a pre-employment drug screen.

    The phrases get the same treatment as the text, so "12-hour" in filters.toml
    finds "12 hour", "12-hour" and JobSpy's "12\\-hour" alike.
    """
    return _boundary_pattern(tuple(text.for_matching(phrase) for phrase in phrases), plural)


@lru_cache(maxsize=None)
def _boundary_pattern(phrases: tuple[str, ...], plural: bool = False) -> re.Pattern[str]:
    """Compiled once per phrase list. Every phrase is matched on word boundaries,
    which is what keeps "Secret" from killing a Secretary role and "no" from
    finding itself inside "technology".

    An empty list matches nothing. Without that guard, emptying a list in
    filters.toml would compile to a pattern that matches everywhere — an empty
    negation list would quietly un-kill every clearance posting.
    """
    phrases = tuple(phrase for phrase in phrases if phrase)
    if not phrases:
        return _NEVER_MATCHES
    alternatives = "|".join(re.escape(phrase) for phrase in phrases)
    suffix = "s?" if plural else ""
    return re.compile(rf"\b(?:{alternatives}){suffix}\b", re.IGNORECASE)
