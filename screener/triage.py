"""Interactive review: one job per screen, one keypress per decision.

Built for the first pass over a pull, where most calls take a few seconds. A
keypress beats typed commands by a wide margin when there are 150 jobs to get
through, and undo exists because a fast pass means the occasional wrong key.
"""

from __future__ import annotations

import json
import os
import sqlite3
import webbrowser
from collections import Counter
from dataclasses import dataclass

import typer

from screener import db, display

# The keys that record a decision, and the mark each one writes.
DECISIONS = {"t": "ignored", "s": "saved", "a": "applied"}
LABELS = {"t": "tossed", "s": "saved", "a": "applied", "n": "skipped"}

MENU = "[t]oss  [s]ave  [a]pplied  [n]ext  [d]escription  [o]pen in browser  [u]ndo  [q]uit"

# Never squeeze the requirements below this. Held low on purpose: every screening
# reason costs two rows once it wraps, so a larger floor here spends a short
# window on requirements you can page through with `d` and drops the model's
# argument entirely, which you cannot get back without leaving the review.
MIN_EXCERPT_ROWS = 6
# Rows used besides the header, excerpt and menu: the two rules around the
# excerpt, and the row the cursor sits on after the menu.
_FRAME_ROWS = 3
# What the excerpt drops to in a window too short for even the header and the
# menu. Something has to give, and it is not the line naming the employer.
_EXCERPT_FLOOR = 3


@dataclass
class LastMove:
    """Enough to put things back exactly as they were before the last keypress."""

    index: int
    job_id: int
    previous_status: str
    key: str


def run(conn: sqlite3.Connection, job_ids: list[int]) -> None:
    index = 0
    full_description = False
    last: LastMove | None = None
    tally: Counter[str] = Counter()

    try:
        while index < len(job_ids):
            # Re-read each time rather than trusting the starting snapshot, because
            # an undo can have changed this row since the queue was built.
            row = db.get_job(conn, job_ids[index])
            counter = f"[{index + 1}/{len(job_ids)}]  #{row['id']}"
            width, max_rows, max_reasons = _fit_to_window(row, counter, full_description)

            clear_screen()
            typer.echo(counter)
            typer.echo(
                display.card(
                    row,
                    full_description=full_description,
                    max_rows=max_rows,
                    width=width,
                    max_reasons=max_reasons,
                )
            )
            typer.echo(MENU)

            key = typer.getchar().lower()
            full_description = False

            if key in DECISIONS or key == "n":
                last = LastMove(index, row["id"], row["status"], key)
                if key in DECISIONS:
                    db.set_status(conn, row["id"], DECISIONS[key])
                tally[key] += 1
                index += 1
            elif key == "d":
                full_description = True
            elif key == "o":
                webbrowser.open(row["url"])
            elif key == "u" and last is not None:
                db.set_status(conn, last.job_id, last.previous_status)
                tally[last.key] -= 1
                index = last.index
                last = None  # one level only; a second undo would need a history
            elif key == "q":
                break
            # Anything else, arrow keys included, redraws the same job.
    except KeyboardInterrupt:
        pass  # Ctrl+C is a normal way to stop; every decision is already saved

    clear_screen()
    summary = ", ".join(f"{tally[key]} {LABELS[key]}" for key in LABELS if tally[key])
    typer.echo(f"went through {index} of {len(job_ids)}: {summary or 'no decisions'}")
    if index < len(job_ids) or tally["n"]:
        # Skipping leaves a job marked `new`, so it comes back next time on purpose.
        typer.echo("run `screener review` again to continue - skipped jobs are still in the queue")
    if tally["s"]:
        typer.echo("your shortlist: screener list --marked saved")


def _fit_to_window(
    row: sqlite3.Row, counter: str, full_description: bool
) -> tuple[int, int | None, int | None]:
    """Text width, excerpt height, and how many screening reasons fit.

    Measured on every redraw, so resizing the window mid-review takes effect on
    the next keypress. The excerpt gets whatever the title, flags and menu leave
    over, which keeps the menu on screen rather than scrolled off the top.

    Screening reasons are the first thing given up when the window is short.
    A verdict plus four wrapped reasons runs the header past twenty rows, which
    pushed the title, company and location off the top of an ordinary window;
    `screener show` still has them in full.
    """
    width, rows = display.terminal_size()
    if full_description:
        return width, None, None

    total = len(json.loads(row["reasons"] or "[]"))
    for shown in range(total, -1, -1):
        fixed = display.screen_rows([counter, *display.header(row, shown, width), MENU], width) + _FRAME_ROWS
        if rows - fixed >= MIN_EXCERPT_ROWS:
            return width, rows - fixed, shown
    fixed = display.screen_rows([counter, *display.header(row, 0, width), MENU], width) + _FRAME_ROWS
    return width, max(_EXCERPT_FLOOR, rows - fixed), 0


def clear_screen() -> None:
    """Typer has no clear() of its own, and it no longer installs click, so this
    asks the shell. `cls` is the Windows spelling."""
    os.system("cls" if os.name == "nt" else "clear")
