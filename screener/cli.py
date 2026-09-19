"""The terminal interface. A thin wrapper — every decision lives in core, filters
or db, so that none of it has to be rewritten if a second front end ever appears.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import typer

from screener import core, db, display, export, llm, screen, settings, triage
from screener.config import FILTERS_FILE, SEARCHES_FILE, ProjectRootNotFound, project_root

app = typer.Typer(add_completion=False, help="Pull IT job postings, kill the noise, review the rest.")

# `saved` is the shortlist: worth a closer look, not yet applied to. `expired`
# means the posting itself is gone, which is worth recording rather than
# deleting: a role that is pulled and relisted is the pattern worth seeing.
MARKS = ("new", "saved", "applied", "rejected", "ignored", "expired")
VERDICTS = ("pass", "maybe", "fail")

# Widths sum to 75; five single-space separators bring the row to exactly 80.
COLUMNS = (("ID", 4), ("SCORE", 5), ("TITLE", 24), ("COMPANY", 15), ("LOCATION", 12), ("FLAGS", 15))


@app.command()
def pull(
    screen: bool = typer.Option(True, "--screen/--no-screen", help="Run the Claude screening pass."),
) -> None:
    """Ingest from the job boards, filter mechanically, and store the survivors."""
    filter_config, search_config = core.load_configs(_root())
    conn = _open_db()

    report = core.run_pull(conn, filter_config, search_config, progress=typer.echo)

    typer.echo("")
    typer.echo(f"pulled      {report.pulled}")
    typer.echo(f"duplicates  {report.duplicates}")
    typer.echo(f"reposts     {report.reposts}")
    typer.echo(f"killed      {report.killed_total}")
    for rule, count in report.killed.most_common(10):
        typer.echo(f"              {count:>4}  {rule}")
    typer.echo(f"kept        {report.kept}  ({report.kept_secondary} secondary-market)")
    typer.echo(
        f"gate        {report.gate_allowed} allowed, {report.gate_denied} denied, "
        f"{report.gate_ambiguous} ambiguous"
    )
    if report.gate_ambiguous:
        typer.echo(
            f"classify    {report.classified_it} IT, {report.classified_not} not IT, "
            f"{report.classify_unanswered} unanswered (kept)"
        )

    for failure in report.failed_searches:
        typer.secho(f"search failed: {failure}", fg=typer.colors.YELLOW)

    if screen:
        typer.echo("")
        _run_screening(conn)


@app.command("list")
def list_jobs(
    status: str = typer.Option(None, "--status", help="Screening verdict: pass | maybe | fail."),
    flag: str = typer.Option(None, "--flag", help="Only jobs carrying this flag, e.g. contract."),
    marked: str = typer.Option("new", "--marked", help="Review state: new | saved | applied | rejected | ignored | all."),
    market: str = typer.Option("primary", "--market", help="primary | secondary | all."),
    limit: int = typer.Option(50, "--limit"),
) -> None:
    """The review queue. Defaults to unreviewed pass/maybe roles in the main market."""
    conn = _open_db()
    rows = db.list_jobs(
        conn,
        verdicts=[status] if status else ["pass", "maybe"],
        status=None if marked == "all" else marked,
        flag=flag,
        market=market,
        limit=limit,
    )

    if not rows:
        typer.echo("nothing to review")
        return

    typer.echo(" ".join(name.ljust(width) for name, width in COLUMNS))
    typer.echo(" ".join("-" * width for _, width in COLUMNS))
    for row in rows:
        typer.echo(_format_row(row))


@app.command()
def show(job_id: int = typer.Argument(..., metavar="ID")) -> None:
    """Full posting, plus whatever the screener concluded about it."""
    conn = _open_db()
    row = db.get_job(conn, job_id)
    if row is None:
        typer.secho(f"no job with id {job_id}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    width, _ = display.terminal_size()
    typer.echo(display.card(row, full_description=True, width=width))


@app.command()
def review(
    marked: str = typer.Option("new", "--marked", help="Which pile to step through: new, or saved to revisit the shortlist."),
    flag: str = typer.Option(None, "--flag", help="Only jobs carrying this flag."),
    market: str = typer.Option("primary", "--market", help="primary | secondary | all."),
) -> None:
    """Step through the queue one job at a time: toss it, save it, or read deeper."""
    conn = _open_db()
    rows = db.list_jobs(
        conn,
        verdicts=["pass", "maybe"],
        status=None if marked == "all" else marked,
        flag=flag,
        market=market,
        limit=10_000,
    )
    if not rows:
        typer.echo("nothing to review")
        return
    triage.run(conn, [row["id"] for row in rows])


@app.command()
def mark(
    job_id: int = typer.Argument(..., metavar="ID"),
    state: str = typer.Argument(..., metavar="STATE", help="saved | applied | rejected | ignored | expired | new"),
) -> None:
    """Record what you did about a job."""
    if state not in MARKS:
        typer.secho(f"state must be one of: {', '.join(MARKS)}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    conn = _open_db()
    if not db.set_status(conn, job_id, state):
        typer.secho(f"no job with id {job_id}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.echo(f"{job_id} -> {state}")


@app.command("export")
def export_jobs(
    marked: str = typer.Option("saved", "--marked", help="Which pile to export: saved, applied, ..."),
    file_format: str = typer.Option("html", "--format", help="html (clickable links) | csv (spreadsheet)."),
    open_file: bool = typer.Option(False, "--open", help="Open the file once it's written."),
) -> None:
    """Write a pile of jobs to a file with links, ready to work through and apply."""
    if file_format not in ("html", "csv"):
        typer.secho("format must be html or csv", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if marked not in MARKS:
        typer.secho(f"marked must be one of: {', '.join(MARKS)}", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    conn = _open_db()
    # Every market: a saved LA role belongs on the list you apply from.
    rows = db.list_jobs(conn, status=marked, market="all", limit=10_000)
    if not rows:
        typer.echo(f"no jobs marked {marked}")
        return

    path = export.default_path(_root() / "exports", marked, file_format)
    path.parent.mkdir(exist_ok=True)
    if file_format == "csv":
        export.write_csv(rows, path)
    else:
        export.write_html(rows, path, marked)

    typer.echo(f"wrote {len(rows)} jobs to {path}")
    if open_file:
        typer.launch(str(path))


@app.command()
def refilter(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would change without writing anything."),
) -> None:
    """Re-run filters.toml over every stored job. Your marks are never changed."""
    filter_config, _ = core.load_configs(_root())
    conn = _open_db()
    report = core.refilter(conn, filter_config, dry_run=dry_run)

    if not report.classify_available:
        typer.secho("classify skipped: OPENROUTER_API_KEY missing from .env; ambiguous titles are kept", fg=typer.colors.YELLOW)
    typer.echo(f"checked       {report.checked}")
    typer.echo(f"newly killed  {sum(report.killed.values())}")
    for rule, count in report.killed.most_common():
        typer.echo(f"                {count:>4}  {rule}")
    typer.echo(f"revived       {report.revived}")
    typer.echo(f"remote label  {report.remote_removed} removed, {report.remote_added} added")
    for flag, count in report.flags_added.most_common():
        typer.echo(f"flag added    {count:>4}  {flag}")
    for flag, count in report.flags_removed.most_common():
        typer.echo(f"flag removed  {count:>4}  {flag}")

    if report.marked_changes:
        typer.echo("")
        typer.echo(f"jobs you had already marked that changed ({len(report.marked_changes)}):")
        for change in report.marked_changes:
            typer.echo(
                f"  #{change.job_id:<4} {change.status:<8} {_fit(change.title, 30)} "
                f"{_fit(change.location or '-', 16)} {change.before} -> {change.after}"
            )
        typer.echo("their marks are unchanged, and `screener show <id>` still opens them")

    if dry_run:
        typer.echo("")
        typer.secho("dry run: nothing was written", fg=typer.colors.YELLOW)


@app.command()
def killed(
    since: str = typer.Option("7d", "--since", help="How far back: 7d, 24h, 2w."),
    per_rule: int = typer.Option(12, "--per-rule", help="Titles to show under each rule."),
) -> None:
    """What the gate and filters killed recently, by rule and title. The audit
    for the silent failure: a job you wanted that a rule ate."""
    cutoff = _cutoff(since)
    conn = _open_db()
    rows = db.killed_since(conn, cutoff)
    if not rows:
        typer.echo(f"nothing killed since {cutoff[:16]}")
        return

    by_rule: dict[str, list[tuple[str, int]]] = {}
    for rule, title, count in rows:
        by_rule.setdefault(rule, []).append((title, count))

    for rule, titles in sorted(by_rule.items(), key=lambda item: -sum(n for _, n in item[1])):
        total = sum(n for _, n in titles)
        typer.echo(f"\n{rule}  ({total})")
        for title, count in titles[:per_rule]:
            typer.echo(f"  {count:>4}  {_fit(title, 70).rstrip()}")
        if len(titles) > per_rule:
            typer.echo(f"        +{len(titles) - per_rule} more titles")


@app.command("screen")
def screen_command(
    limit: int = typer.Option(None, "--limit", help="Screen only this many, to price a run before committing to it."),
) -> None:
    """Score the pending queue against your profile. This is the step that costs money."""
    _run_screening(_open_db(), limit)


@app.command()
def stats() -> None:
    """What the pipeline did: how much came in, what killed it, what survived."""
    conn = _open_db()
    stages = db.stage_counts(conn)
    total = sum(stages.values())

    typer.echo(f"stored      {total}")
    typer.echo(f"  killed    {stages.get(db.STAGE_KILLED, 0)}")
    typer.echo(f"  pending   {stages.get(db.STAGE_PENDING, 0)}")
    typer.echo(f"  screened  {stages.get(db.STAGE_SCREENED, 0)}")

    _section("killed by", db.kill_counts(conn))
    _section("by market", db.market_counts(conn))
    _section("by verdict", db.verdict_counts(conn))
    _section("by mark", db.status_counts(conn))


@app.command()
def filters() -> None:
    """Print the filter config currently in force."""
    filter_config, _ = core.load_configs(_root())

    typer.echo(f"govcon filter {'on' if filter_config.govcon_enabled else 'off'}  (screener govcon on|off)\n")

    typer.echo(f"blocked employers ({len(filter_config.blocked_companies)})")
    for company in filter_config.blocked_companies:
        typer.echo(f"  {company}")

    typer.echo(f"\nkill keywords ({len(filter_config.kill_keywords)})")
    for keyword in filter_config.kill_keywords:
        typer.echo(f"  {keyword}")
    typer.echo(
        f"  negated within {filter_config.window_chars} chars by "
        f"{len(filter_config.negation_before)} before / "
        f"{len(filter_config.negation_after)} after cues"
    )

    typer.echo("\nlocation")
    typer.echo(f"  primary    {', '.join(sorted(filter_config.primary_locations))}")
    typer.echo(
        f"  secondary  {', '.join(sorted(filter_config.secondary_locations))} "
        f"-> {filter_config.secondary_tag}"
    )
    typer.echo(f"  remote US  {'kept' if filter_config.keep_remote_us else 'killed'}")
    rules = filter_config.remote
    typer.echo(
        f"  remote check  {len(rules.contradicted_by)} on-site phrases, "
        f"{len(rules.stated_by)} remote phrases, {len(rules.loosely_stated_by)} loose"
    )

    typer.echo("\nsoft flags")
    for rule in filter_config.keyword_flags:
        typer.echo(f"  {rule.flag}: {len(rule.keywords)} keywords")
    typer.echo(f"  contract: job_type in {', '.join(sorted(filter_config.contract_job_types))}")
    typer.echo(f"  low-pay:  below ${filter_config.low_pay_below:,.0f}/yr")


titles_app = typer.Typer(help="The job titles the pull searches for.")
locations_app = typer.Typer(help="Where the pull searches, and which postings the location rule keeps.")
app.add_typer(titles_app, name="titles")
app.add_typer(locations_app, name="locations")


@titles_app.callback(invoke_without_command=True)
def titles_default(ctx: typer.Context) -> None:
    """With no subcommand, list the titles."""
    if ctx.invoked_subcommand is None:
        titles_list()


@titles_app.command("list")
def titles_list() -> None:
    """The titles, and how many queries a pull will run."""
    _, search_config = core.load_configs(_root())
    typer.echo(f"titles ({len(search_config.titles)}), {search_config.titles_per_search} per query")
    for title in search_config.titles:
        typer.echo(f"  {title}")
    places = len(search_config.locations)
    typer.echo(f"\n{len(search_config.searches)} queries per pull ({places} location{'s' if places != 1 else ''})")


@titles_app.command("add")
def titles_add(titles: list[str] = typer.Argument(..., metavar="TITLE...", help='Quote multi-word titles: "network administrator".')) -> None:
    """Search for more titles. Each one is also let through the relevance gate."""
    root = _root()
    added, present = settings.add_titles(root / SEARCHES_FILE, titles)
    for title in added:
        typer.echo(f"added    {title}")
    for title in present:
        typer.echo(f"present  {title}")

    filter_config, _ = core.load_configs(root)
    for title in added:
        for conflict in settings.title_conflicts(title, filter_config):
            typer.secho(
                f'warning  "{title}" matches {conflict} in {FILTERS_FILE}; those jobs will still be killed',
                fg=typer.colors.YELLOW,
            )
    if added:
        _refilter_hint()


@titles_app.command("remove")
def titles_remove(titles: list[str] = typer.Argument(..., metavar="TITLE...")) -> None:
    """Stop searching for titles. Jobs already stored are kept."""
    removed, missing = settings.remove_titles(_root() / SEARCHES_FILE, titles)
    for title in removed:
        typer.echo(f"removed    {title}")
    for title in missing:
        typer.secho(f"not found  {title}", fg=typer.colors.YELLOW)
    if removed:
        _refilter_hint()
    if missing and not removed:
        raise typer.Exit(code=1)


@locations_app.callback(invoke_without_command=True)
def locations_default(ctx: typer.Context) -> None:
    """With no subcommand, list the locations."""
    if ctx.invoked_subcommand is None:
        locations_list()


@locations_app.command("list")
def locations_list() -> None:
    """Each location, its market, and the tokens that keep its postings."""
    _, search_config = core.load_configs(_root())
    if not search_config.locations:
        typer.echo("no locations: add one with `screener locations add`")
        return
    for place in search_config.locations:
        labels = [place.market] + (["remote"] if place.remote else [])
        typer.echo(f"{place.name}  ({', '.join(labels)})")
        if place.match:
            typer.echo(f"  keeps  {', '.join(place.match)}")
        elif place.remote:
            typer.echo("  keeps  remote jobs anywhere in the US")


@locations_app.command("add")
def locations_add(
    name: str = typer.Argument(..., metavar="NAME", help='As Indeed takes it: "Austin, TX", "United States".'),
    match: list[str] = typer.Option(None, "--match", help="A location token to keep; repeat for more. Defaults to the state."),
    remote: bool = typer.Option(False, "--remote", help="Search remote jobs here."),
    secondary: bool = typer.Option(False, "--secondary", help="Keep them tagged and out of the default queue."),
) -> None:
    """Search somewhere new, and keep what it finds."""
    try:
        place = settings.add_location(_root() / SEARCHES_FILE, name, match, remote, secondary)
    except settings.SettingsError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error

    typer.echo(f"added  {place['name']}  ({place['market']}{', remote' if place['remote'] else ''})")
    if place["match"]:
        typer.echo(f"jobs located in {', '.join(t.upper() if len(t) == 2 else t for t in place['match'])} will be kept")
        if not match:
            typer.echo("narrow it with --match, e.g. --match austin --match \"round rock\"")
    else:
        typer.echo("remote jobs anywhere in the US will be kept")
    _refilter_hint()


@locations_app.command("remove")
def locations_remove(name: str = typer.Argument(..., metavar="NAME")) -> None:
    """Stop searching a location. Its tokens stop keeping postings too."""
    try:
        stored = settings.remove_location(_root() / SEARCHES_FILE, name)
    except settings.SettingsError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error
    typer.echo(f"removed  {stored}")
    _refilter_hint()


@app.command()
def govcon(
    state: str = typer.Argument(None, metavar="[on|off]", help="Leave out to see the current setting."),
) -> None:
    """Switch the government-contractor and clearance filter on or off."""
    path = _root() / FILTERS_FILE
    employers, keywords = settings.govcon_sizes(path)
    if state is None:
        filter_config, _ = core.load_configs(_root())
        typer.echo(f"govcon filter is {'on' if filter_config.govcon_enabled else 'off'}")
        typer.echo(f"  {employers} employers, {keywords} clearance keywords, in [govcon] in {FILTERS_FILE}")
        return
    if state not in ("on", "off"):
        typer.secho("state must be on or off", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    try:
        previous = settings.set_govcon(path, state == "on")
    except settings.SettingsError as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error
    if previous == (state == "on"):
        typer.echo(f"govcon filter was already {state}")
        return
    verb = "now kills" if state == "on" else "no longer kills"
    typer.echo(f"govcon filter {state}: {verb} {employers} employers and {keywords} clearance keywords")
    _refilter_hint()


def _refilter_hint() -> None:
    typer.echo("run `screener refilter --dry-run` to see what this does to stored jobs, then `screener refilter`")


def _run_screening(conn, limit: int | None = None) -> None:
    """Shared by `pull` and `screen` so the two can never drift apart."""
    root = _root()
    core.load_configs(root)  # for the .env load; the key lives there, not in config

    client = llm.client_from_env()
    if client is None:
        typer.secho("screening skipped: OPENROUTER_API_KEY missing from .env", fg=typer.colors.YELLOW)
        return
    try:
        facts = screen.load_facts(root)
    except screen.ProfileNotFound as error:
        typer.secho(f"screening skipped: {error}", fg=typer.colors.YELLOW)
        return

    pending = len(db.pending_jobs(conn))
    if not pending:
        typer.echo("nothing pending to screen")
        return
    typer.echo(f"screening {min(pending, limit) if limit else pending} of {pending} pending")
    typer.echo("")

    report = screen.screen_pending(conn, client, facts, limit, progress=typer.echo)

    typer.echo("")
    typer.echo(f"screened    {report.screened}")
    for verdict in (screen.PASS, screen.MAYBE, screen.FAIL):
        typer.echo(f"  {verdict:<9} {report.verdicts[verdict]}")
    if report.failed:
        typer.secho(f"failed      {report.failed}  (left pending, run again to retry)", fg=typer.colors.YELLOW)
    typer.echo(f"tokens      {report.prompt_tokens:,} in, {report.completion_tokens:,} out")
    typer.echo(f"cost        ${report.cost:.2f}")


def _root() -> Path:
    """Resolved per command rather than at import, so a missing filters.toml
    prints one line instead of a traceback from the import machinery."""
    try:
        return project_root()
    except ProjectRootNotFound as error:
        typer.secho(str(error), fg=typer.colors.RED)
        raise typer.Exit(code=1) from error


def _cutoff(since: str) -> str:
    """"7d" -> the ISO timestamp seven days ago, in the form first_seen uses."""
    match = re.fullmatch(r"(\d+)([hdw])", since.strip().lower())
    if not match:
        typer.secho("--since looks like 7d, 24h or 2w", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    amount, unit = int(match.group(1)), match.group(2)
    delta = {"h": timedelta(hours=amount), "d": timedelta(days=amount), "w": timedelta(weeks=amount)}[unit]
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="seconds")


def _open_db() -> sqlite3.Connection:
    return db.connect(_root() / "jobs.db")


def _format_row(row: sqlite3.Row) -> str:
    values = (
        str(row["id"]),
        str(row["match_score"]) if row["match_score"] is not None else "-",
        row["title"],
        row["company"],
        # Boards stamp remote postings with the employer's HQ, so the raw
        # location reads as a rejection when it's the reason the row survived.
        "Remote" if display.is_remote(row) else row["location"],
        ",".join(json.loads(row["flags"])),
    )
    return " ".join(_fit(value, width) for value, (_, width) in zip(values, COLUMNS))


def _fit(value: str, width: int) -> str:
    """Pad or truncate to exactly `width`. ASCII ellipsis on purpose — a Windows
    console on a legacy code page can't print the single-character one."""
    value = value.replace("\n", " ")
    if len(value) <= width:
        return value.ljust(width)
    return value[: width - 2] + ".."


def _section(title: str, pairs: list[tuple[str, int]]) -> None:
    if not pairs:
        return
    typer.echo(f"\n{title}")
    for label, count in pairs:
        typer.echo(f"  {count:>5}  {label}")


if __name__ == "__main__":
    app()
