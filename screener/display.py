"""How one job is laid out on screen.

Shared by `screener show` and `screener review` so a posting looks the same
wherever you meet it. Separators are plain ASCII because a Windows console on a
legacy code page mangles anything fancier.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import sqlite3
import textwrap

from screener import text

DEFAULT_WIDTH = 80
MIN_WIDTH = 40

# A heading is short and not a sentence. Longer than this and it's body text.
_HEADING_MAX_WORDS = 8
_HEADING_MAX_CHARS = 70

_REQUIREMENTS = re.compile(
    r"qualification|requirement|what you('ll| will)? (need|bring|have)"
    r"|what (we|you)('re| are) looking for|who (we|you)('re| are)|\byou are\b|\byou have\b"
    r"|ideal candidate|must have|nice to have|bonus points|preferred|skills|experience"
    r"|education|knowledge|competenc|certification",
    re.IGNORECASE,
)
# Headings that contain a requirements word but open a section that doesn't decide
# a toss: "Physical Requirements", "Compensation & Experience", "Travel Requirements".
_NOT_REQUIREMENTS = re.compile(
    r"physical|work environment|benefit|compensation|salary|\bpay\b|about|equal|eeo|perks"
    r"|what we offer|why join|responsibilit|duties|what you('ll| will) do|day to day"
    r"|schedule|travel|summary|overview|accommodation|culture",
    re.IGNORECASE,
)

# The order sections are shown in. A posting's own order can put a 70-row
# "Knowledge and Abilities" list ahead of the five-line "Required Qualifications"
# that actually decides a toss, and the window fills before reaching it. So the
# must-haves come first, then knowledge and skills lists, then the nice-to-haves.
_OPTIONAL_SECTION = re.compile(r"preferred|bonus|nice to have|desired|\bplus\b", re.IGNORECASE)
_CORE_SECTION = re.compile(
    r"requir|qualification|minimum|basic|must have|what you('ll| will)? need|looking for"
    r"|ideal candidate|who you are|\byou are\b|\byou have\b",
    re.IGNORECASE,
)

# The pay/benefits/EEO boilerplate after the requirements often has no heading of
# its own, only a paragraph that opens with or mentions one of these.
_BOILERPLATE = re.compile(
    r"^(compensation|salary|pay range|benefits|equal (employment )?opportunity)"
    r"|401\(k\)|paid time off|\bpto\b|medical, dental|benefits (plan|package|include)"
    r"|equal opportunity employer|\bwe offer\b",
    re.IGNORECASE,
)
# Lines that state a requirement, for postings with no requirements heading at all.
_REQUIREMENT_CUE = re.compile(
    r"\byears?\b|degree|diploma|certif|clearance|citizen|driver|required|\bmust\b"
    r"|experience (with|in)|proficien|knowledge of|familiarity",
    re.IGNORECASE,
)
_CUE_LINE_MAX_CHARS = 400


def card(
    row: sqlite3.Row,
    full_description: bool = False,
    max_rows: int | None = None,
    width: int = DEFAULT_WIDTH,
    max_reasons: int | None = None,
) -> str:
    width = max(MIN_WIDTH, width)
    rule = "-" * width
    if full_description:
        body = "\n".join(_wrap(text.lines(row["description"]), width))
    else:
        body = excerpt(row["description"], max_rows=max_rows, width=width)
    return "\n".join([*header(row, max_reasons, width), rule, body or "(no description)", rule])


def header(row: sqlite3.Row, max_reasons: int | None = None, width: int = DEFAULT_WIDTH) -> list[str]:
    remote = "  (remote)" if is_remote(row) else ""
    lines = [
        row["title"],
        f"{row['company']} - {row['location']}{remote}",
        f"{row['site']} | {row['date_posted'] or 'no date'} | {salary_text(row)}",
        row["url"],
        "",
    ]

    if row["killed_by"]:
        lines.append(f"killed   {row['killed_by']}")
    lines.append(f"market   {row['market']}    marked {row['status']}")
    lines.append(f"flags    {', '.join(json.loads(row['flags'])) or '-'}")

    if row["verdict"]:
        lines.append("")
        lines.append(f"verdict  {row['verdict']}  (score {row['match_score']})")
        reasons = json.loads(row["reasons"] or "[]")
        # Capped when the window is short: the title and the employer must not
        # be pushed off the top of the screen to make room for these.
        shown = reasons if max_reasons is None else reasons[:max_reasons]
        # Wrapped like the excerpt is. An unwrapped reason runs past the rule and
        # wraps at the terminal edge instead, losing the bullet's indent.
        lines.extend(_wrap([f"  - {reason}" for reason in shown], width))
        if len(shown) < len(reasons):
            left = len(reasons) - len(shown)
            lines.append(f"  (+{left} more - screener show {row['id']})")
        if row["salary_note"]:
            lines.append(f"salary   {row['salary_note']}")
    return lines


def excerpt(description: str | None, max_rows: int | None = None, width: int = DEFAULT_WIDTH) -> str:
    """The requirements, wrapped to `width` and cut to `max_rows` rows.

    When they don't fit, the last row says how many were left out, so a short
    excerpt never passes for the whole list.
    """
    rows = _wrap(requirements(description), max(MIN_WIDTH, width))
    if max_rows is None or len(rows) <= max_rows:
        return "\n".join(rows)
    shown = rows[: max(1, max_rows - 1)]
    return "\n".join([*shown, f"(+{len(rows) - len(shown)} more rows - press d)"])


def requirements(description: str | None) -> list[str]:
    """The lines of a posting that say what it requires.

    Every requirements section is collected — Required, Knowledge, Preferred,
    "Bonus points" — each running to the next heading or benefits paragraph, and
    shown must-haves first. A posting with no requirements heading falls back to
    the lines that read like requirements, and one with neither shows from the top.
    """
    lines = text.lines(description)
    sections: list[list[str]] = []
    i = 0
    while i < len(lines):
        if not _is_requirements_heading(lines[i]):
            i += 1
            continue
        section = [lines[i]]
        i += 1
        while i < len(lines) and not _ends_section(lines[i]):
            section.append(lines[i])
            i += 1
        # A heading followed straight by another heading has nothing to show.
        if len(section) > 1:
            sections.append(section)

    if sections:
        sections.sort(key=_section_priority)  # stable: document order within a priority
        return [line for section in sections for line in section]
    cues = [line for line in lines if _REQUIREMENT_CUE.search(line) and len(line) <= _CUE_LINE_MAX_CHARS]
    return cues or lines


def is_remote(row: sqlite3.Row) -> bool:
    """The remote check's answer. Rows stored before the check existed don't have
    one yet, so they fall back to the board's own claim until `screener refilter`."""
    checked = row["remote"]
    return bool(row["is_remote"] if checked is None else checked)


def terminal_size() -> tuple[int, int]:
    """The usable width and height of the window.

    The width is one column short of the real one: some Windows consoles add a
    blank row when a line fills the window exactly, and every height sum built
    on that is then wrong by a row per wrapped line.
    """
    columns, rows = shutil.get_terminal_size()
    return max(MIN_WIDTH, columns - 1), rows


def screen_rows(lines: list[str], width: int) -> int:
    """How many terminal rows these lines take once the terminal wraps them."""
    return sum(max(1, math.ceil(len(line) / width)) for line in lines)


def salary_text(row: sqlite3.Row) -> str:
    if not row["salary_min"] and not row["salary_max"]:
        return "no salary listed"
    low = f"{row['salary_min']:,.0f}" if row["salary_min"] else "?"
    high = f"{row['salary_max']:,.0f}" if row["salary_max"] else "?"
    return f"{low}-{high} {row['salary_interval'] or ''}".strip()


def _heading(line: str) -> str | None:
    """The heading text if `line` looks like a section heading, otherwise None.

    Boards mark headings every way markdown allows — **bold**, *italic*, # hashes,
    a trailing colon, ALL CAPS. What they share is being short and not a sentence,
    so a bullet, or anything ending in a full stop, is never a heading. That rule
    is what stops "**This position is on-site 5 days per week.**" ending a
    requirements section early.
    """
    stripped = line.strip()
    if text.is_bullet(stripped):
        return None
    bare = stripped.strip("*#_ ")
    ends_with_colon = bare.endswith(":")
    bare = bare.rstrip(":").strip("*_ ")
    if not bare or len(bare) > _HEADING_MAX_CHARS or bare.endswith((".", ",", ";")):
        return None
    words = len(bare.split())
    if words > _HEADING_MAX_WORDS:
        return None
    emphasised = stripped.startswith(("*", "_", "#"))
    all_caps = bare.isupper() and words <= 6
    return bare if (ends_with_colon or emphasised or all_caps) else None


def _is_requirements_heading(line: str) -> bool:
    heading = _heading(line)
    return bool(heading and _REQUIREMENTS.search(heading) and not _NOT_REQUIREMENTS.search(heading))


def _ends_section(line: str) -> bool:
    """Any heading ends a section; a requirements heading then starts its own, so
    each can be ordered separately. So does a benefits-style paragraph, but never a
    bullet: a requirement can mention PTO without being boilerplate."""
    if _heading(line) is not None:
        return True
    return not text.is_bullet(line) and bool(_BOILERPLATE.search(line.strip("*_ ")))


def _section_priority(section: list[str]) -> int:
    heading = _heading(section[0]) or ""
    if _OPTIONAL_SECTION.search(heading):
        return 2
    if _CORE_SECTION.search(heading):
        return 0
    return 1


def _wrap(lines: list[str], width: int) -> list[str]:
    """Wrap rather than cut: a requirement's last clause is often the one that
    matters ("... or equivalent experience"). Continuation rows are indented so a
    wrapped bullet still reads as one bullet."""
    rows: list[str] = []
    for line in lines:
        rows.extend(textwrap.wrap(line.replace("**", ""), width, subsequent_indent="    ") or [""])
    return rows
