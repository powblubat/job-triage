"""Text cleanup shared by the filters and the display.

JobSpy stores each description as markdown converted from the board's HTML, and
the converter backslash-escapes punctuation: "3\\-5 years", "A\\+", "On\\-Site".
Left alone, those escapes print as clutter and, worse, make filter phrases miss —
"12-hour" never matched "12\\-hour". Anything that reads a description goes
through here first.
"""

from __future__ import annotations

import re

_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|&$~<>/=])")
_CURLY_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
_WHITESPACE = re.compile(r"\s+")
_BULLET = re.compile(r"^(?:[*+\-•·●▪◦]|\d+[.)])\s")
_SENTENCE_END = re.compile(r"[.:;!?]$")
_EMPHASIS = re.compile(r"[*_]+")


def clean(text: str | None) -> str:
    """Undo the markdown escapes and straighten curly quotes.

    Curly quotes matter because postings write "What you’ll need" and "What you'll
    need" interchangeably, and a heading pattern written with one misses the other.
    """
    return _ESCAPE.sub(r"\1", text or "").translate(_CURLY_QUOTES)


def lines(text: str | None) -> list[str]:
    """The non-blank lines of a description, cleaned and with split sentences rejoined."""
    return reflow([line.strip() for line in clean(text).splitlines() if line.strip()])


def reflow(raw_lines: list[str]) -> list[str]:
    """Rejoin sentences a board split across lines.

    LinkedIn puts bold spans in their own paragraphs, so a posting can arrive as
    "is currently seeking a" / "Technician" / ".". Only the unambiguous cases are
    joined: a lone punctuation mark, or a line starting lowercase after a line with
    no closing punctuation. A line starting with a capital could just as easily be
    a new sentence or a heading, so it is left alone.
    """
    joined: list[str] = []
    for line in raw_lines:
        bare = line.strip("*_ ")
        if joined and not _BULLET.match(line):
            previous = joined[-1].rstrip("*_ ")
            if bare in {".", ",", ";", ":"}:
                joined[-1] = joined[-1].rstrip() + bare
                continue
            if bare[:1].islower() and previous and not _SENTENCE_END.search(previous):
                joined[-1] = f"{joined[-1].rstrip()} {line.strip()}"
                continue
        joined.append(line)
    return joined


def is_bullet(line: str) -> bool:
    return bool(_BULLET.match(line.strip()))


def for_matching(text: str | None) -> str:
    """One canonical form for phrase matching.

    Lowercase, hyphens as spaces, whitespace collapsed — so "On\\-Site", "on-site"
    and "on site" all come out as "on site", and one phrase in filters.toml covers
    every spelling of it.

    Emphasis markers become spaces rather than vanishing. Boards run bold spans
    together with nothing else between them, and deleting the markers turned
    "Clearance: Top Secret****Location" into "top secretlocation", which hid a Top
    Secret posting from the clearance filter.
    """
    flat = _EMPHASIS.sub(" ", clean(text).lower()).replace("-", " ")
    return _WHITESPACE.sub(" ", flat).strip()
