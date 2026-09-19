"""Writes a pile of jobs to a file you can work from outside the terminal.

HTML is the default because the point of an export is clicking through to apply:
a link in an HTML page is clickable everywhere, while a URL in a CSV is only
clickable in some spreadsheet apps.
"""

from __future__ import annotations

import csv
import html
import json
import sqlite3
from datetime import date
from pathlib import Path

from screener import display

CSV_COLUMNS = ("id", "title", "company", "location", "salary", "flags", "date_posted", "site", "url")


def default_path(export_dir: Path, marked: str, file_format: str) -> Path:
    """Dated, so this week's export doesn't overwrite last week's — the old file is
    the record of what was on the shortlist back then."""
    return export_dir / f"{marked}-jobs-{date.today().isoformat()}.{file_format}"


def write_csv(rows: list[sqlite3.Row], path: Path) -> None:
    # utf-8-sig adds the byte-order mark Excel looks for. Without it, Excel guesses
    # the wrong encoding and "Welltower™" opens as a run of garbage characters.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_COLUMNS)
        for row in rows:
            writer.writerow(
                [
                    row["id"],
                    row["title"],
                    row["company"],
                    _location(row),
                    display.salary_text(row),
                    ", ".join(json.loads(row["flags"])),
                    row["date_posted"] or "",
                    row["site"],
                    row["url"],
                ]
            )


def write_html(rows: list[sqlite3.Row], path: Path, marked: str) -> None:
    items = "\n".join(_html_item(row) for row in rows)
    page = "\n".join(
        [
            "<!doctype html>",
            '<html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{html.escape(marked.capitalize())} jobs - {date.today().isoformat()}</title>",
            f"<style>{_CSS}</style>",
            "</head><body>",
            f"<h1>{html.escape(marked.capitalize())} jobs <span>{len(rows)}</span></h1>",
            "<p class=\"hint\">After applying: <code>screener mark &lt;id&gt; applied</code></p>",
            f"<ol>\n{items}\n</ol>",
            "</body></html>",
        ]
    )
    path.write_text(page, encoding="utf-8")


def _html_item(row: sqlite3.Row) -> str:
    # Every value came from a scraped posting, so all of it is escaped before it
    # touches the page — a title containing markup must show as text, not run.
    title = html.escape(row["title"])
    url = row["url"] or ""
    # Only real web links become clickable. A scraped "javascript:" URL would
    # otherwise run code the moment you click it.
    if url.startswith(("https://", "http://")):
        title = f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">{title}</a>'

    details = " &middot; ".join(
        html.escape(part)
        for part in (row["company"], _location(row), display.salary_text(row))
        if part
    )
    flags = json.loads(row["flags"])
    flag_html = "".join(f'<span class="flag">{html.escape(flag)}</span>' for flag in flags)
    posted = html.escape(row["date_posted"] or "no date")

    return (
        f'<li><div class="title">{title}</div>'
        f'<div class="details">{details}</div>'
        f'<div class="meta">#{row["id"]} &middot; {html.escape(row["site"])} &middot; posted {posted} {flag_html}</div></li>'
    )


def _location(row: sqlite3.Row) -> str:
    return f"Remote ({row['location']})" if display.is_remote(row) else row["location"]


_CSS = """
:root { color-scheme: light dark; --muted: #6b6b6b; --flag: #e8e2d4; --flag-text: #5a4a1f; }
@media (prefers-color-scheme: dark) { :root { --muted: #a0a0a0; --flag: #3a3526; --flag-text: #e6d7a8; } }
body { font: 16px/1.5 system-ui, sans-serif; max-width: 46rem; margin: 2rem auto; padding: 0 1rem; }
h1 { font-size: 1.5rem; margin-bottom: 0.25rem; }
h1 span { color: var(--muted); font-weight: normal; }
.hint { color: var(--muted); margin-top: 0; }
ol { padding-left: 1.5rem; }
li { margin-bottom: 1.1rem; }
.title { font-weight: 600; font-size: 1.05rem; }
.details { margin-top: 0.1rem; }
.meta { color: var(--muted); font-size: 0.875rem; }
.flag { background: var(--flag); color: var(--flag-text); border-radius: 4px; padding: 0 0.4rem; margin-left: 0.3rem; font-size: 0.8rem; }
"""
