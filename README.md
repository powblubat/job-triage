# job-triage

A command-line tool for anyone hunting for IT support work: help desk, desktop
support, IT specialist, junior sysadmin. It pulls postings from Indeed and kills
the noise with rules you can read and edit: government contractors, clearance
requirements, senior titles, trades that happen to be called "technician", and
jobs that call themselves remote but aren't. It then has Claude score what's
left against a written profile of your background. You review the rest in the
terminal, one job per screen.

The scoring is harsh on purpose. A job seeker's judgment gets generous when the
search drags on, and a screener that tells you every posting is a fit is worse
than no screener at all. The model may only treat what's in your profile as
true, and a requirement you don't meet counts against you.

## Requirements

- Python 3.12 or newer.
- An [OpenRouter](https://openrouter.ai/keys) API key for the two model steps:
  a cheap Claude Haiku yes/no on titles the rules can't place, and the Claude
  Sonnet fit score. Both are optional. Without a key the pull still runs and
  stores jobs, unscored. `pull` and `screen` print what the scoring actually cost,
  and `screener screen --limit 5` lets you price a run before committing to it.

## Install

```bash
git clone https://github.com/powblubat/job-triage.git
cd job-triage
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -e .
```

The command is `screener`, and it works from any folder. It finds its config by walking up from
the current folder looking for `filters.toml`, then falls back to the folder it
was installed from. Set `SCREENER_HOME` to point it somewhere else. The job
database, `jobs.db`, is always created next to `filters.toml`.

## Quickstart

1. **Keys.** Copy `.env.example` to `.env` and paste your OpenRouter key in.

2. **Your profile.** Copy `profile/facts.example.md` to `profile/facts.md` and
   fill it in. This file is everything the screener may believe about you, so
   be specific and be honest. Give scope in numbers, say which projects you led
   and which you only helped with, and use the "What this is not" section to
   name the ways your background tends to get inflated. What you leave out is
   scored as a gap. Git ignores `facts.md`.

3. **What to search for.** The shipped config searches for entry-level IT
   support titles in Washington, DC, remote US jobs, and Los Angeles as a
   secondary market. Change it to fit you:

   ```bash
   screener titles                                  # what's searched now
   screener titles add "network administrator" "noc technician"
   screener titles remove intune
   screener locations                               # where, and what's kept
   screener locations add "Austin, TX"
   screener locations remove "Los Angeles, CA"
   ```

4. **The government filter.** It's on by default. It kills about a hundred
   federal contractors and any posting that needs a security clearance. If you
   hold a clearance, or want government work, turn it off:

   ```bash
   screener govcon off
   ```

5. **Pull and review.**

   ```bash
   screener pull        # search, filter, store, then score the survivors
   screener review      # one job per screen
   ```

   In `review`: `s` save, `a` applied, `t` toss, `n` skip, `d` full
   description, `o` open the posting in a browser, `u` undo, `q` quit.

6. **Apply.** `screener export --open` writes your saved jobs to a page of
   clickable links.

## Commands

```
screener pull                       # search -> filter -> store -> score
screener pull --no-screen           # same, without the paid scoring step
screener screen [--limit N]         # score whatever is still pending
screener review                     # one job per screen, local in-person jobs first
screener review --marked saved      # second pass over your shortlist
screener list                       # unreviewed pass/maybe jobs, main market
screener list --status maybe --flag contract
screener list --market secondary    # the secondary-market watchlist
screener show <id>                  # full posting, works on killed rows too
screener mark <id> saved|applied|rejected|ignored|expired|new
screener export --open              # saved jobs as a page of clickable links
screener export --format csv        # same, as a spreadsheet

screener titles [list|add|remove]   # what the pull searches for
screener locations [list|add|remove]
screener govcon [on|off]            # the government-contractor filter
screener filters                    # the filter config currently in force

screener stats                      # what came in, what killed it, what survived
screener killed --since 7d          # what died recently, by rule and title
screener refilter --dry-run         # what a config change would do to stored jobs
screener refilter                   # apply it; your marks are never changed
```

## Configuring

The config is two TOML files, and they're commented throughout. The commands
above edit them for you and leave the comments intact, or you can edit them by
hand. Either way, a change reaches new jobs on the next pull. Run
`screener refilter` to apply it to jobs you already have.

### Titles

`searches.toml`, under `[search]`. Every title is searched in every location
with Indeed's `title:(...)` operator. A bare or quoted term searches the whole
posting: `"help desk"` returned attorneys whose boilerplate said "contact the
help desk", while `title:("help desk" OR helpdesk)` returned 25 of 25 help desk
jobs. Titles are grouped `titles_per_search` to a query (default 5). Each query
returns at most `results_wanted` jobs, so smaller groups find more but make
the pull slower.

A title you search for is also let through the relevance gate (below).
`titles add` warns you if the title would still be killed by a deny phrase or a
title-kill word. "Senior help desk analyst" is one example, because `senior` is
on the kill list.

### Locations

`searches.toml`, as `[[locations]]` blocks. Each location is two things: where
to search, and a `match` list of location tokens that keep a posting. JobSpy
writes locations as `City, ST, Country`, and a posting survives when one of the
comma-separated parts equals a token.

```toml
[[locations]]
name = "Washington, DC"
match = ["dc", "district of columbia", "md", "maryland", "va", "virginia"]

[[locations]]
name = "United States"
remote = true                 # remote jobs, kept anywhere in the US

[[locations]]
name = "Los Angeles, CA"
market = "secondary"          # kept but tagged, and held out of the default queue
match = ["los angeles", "pasadena", "glendale", "burbank"]
```

`screener locations add "Austin, TX"` matches the state (`tx`) by default,
because Indeed's search radius reaches suburbs that post under their own town
names. Narrow it with `--match austin --match "round rock"`. Add `--remote` for
a remote search, or `--secondary` for a market you want to watch without
mixing it into your main queue.

### Filters

`filters.toml` holds every kill and flag rule. It ships tuned for entry-level,
non-cleared IT support work, and three parts are worth reviewing first:

- **`[govcon]`**: federal contractors, and the clearance keywords that kill a
  posting. Switch the whole group with `screener govcon on|off`, or trim the
  lists.
- **`title_kill`**: the title words that mark a job as not yours: `senior`,
  `lead`, `manager`, `developer`, `architect` and so on. Take `senior` and `sr`
  out if you have the years for them.
- **`[flags]`**: soft flags that annotate a job rather than kill it: low pay
  (`low_pay_below`), shift work, contract.

## How a job is judged

Each job takes one path, in order:

1. **Pull**: one Indeed query per title group per location. A query that fails
   (a rate limit or a layout change) is reported and skipped, and the run
   continues.
2. **Dedup**: a hash of title, company and location. A repost hashes the same,
   so a role you've already killed doesn't come back. It's counted as a repost
   instead.
3. **Gate**: is this an IT job at all? `[gate]` in `filters.toml` reads the
   title only. A `deny` phrase kills it (deny beats allow: "Pool Technician / IT
   Support" is a pool job). An `allow` phrase or one of your titles lets it on.
   A title matching neither ("Technician Apprentice") is decided by the
   `ambiguous` setting: `classify` asks Haiku one yes/no question per title and
   caches the answer, while `kill` and `keep` do what they say.
4. **Filter**: the blocklisted employers, then the kill keywords, then the title
   kills, then location. The order decides which rule gets the credit in
   `stats`.
5. **Store**: every row is kept, killed or not, with `killed_by` naming the rule
   that killed it.
6. **Screen**: survivors get a 0–100 fit score, a verdict (`pass`, `maybe` or
   `fail`), up to four short reasons and flags like `degree-required` or
   `seniority-gap`. `review` shows the passes and maybes.

A rule that kills a job you wanted makes no sound. `screener killed --since 7d`
is the audit: every recent kill, grouped by rule and title, so an over-broad
rule shows up in thirty seconds.

### The negation rule

Kill keywords match on word boundaries and are **negation-aware**. A match is
ignored when a cue like `no`, `without`, `is not required` or `ability to
obtain` sits within 60 characters of it. Without this, "no security clearance
required", which is exactly the posting worth reading, would die to the same
rule that kills the cleared reposts. The cue lists and the window size are in
`filters.toml`.

The tradeoff runs one way on purpose. A window that's too wide shows you a job
that should have died. One that's too narrow hides a job you wanted. Widen
before you narrow.

### The remote check

Indeed calls a job remote if the word "remote" appears anywhere in it, so
"provide onsite and remote support" is enough. In one early sample, 124 of the
232 jobs tagged remote were outside the search area and survived only because
of the tag. So the filter asks the posting itself, using the `[remote]` phrase lists. The first match wins:

1. On-site or hybrid wording: not remote, even if it also says remote.
2. Remote stated outright, or "remote" in the title or location: remote.
3. Loose wording ("may be remote, hybrid, or onsite"): kept as remote.
4. No mention of remote at all: remote, since the tag came from the board's own
   listing data.
5. Anything else: not remote.

A job that fails the check goes through the ordinary location rule instead.

## Sources

Indeed only, through [JobSpy](https://github.com/speedyapply/JobSpy). The
others have been tried:

- **Glassdoor**: JobSpy resolves locations through an endpoint Glassdoor has
  removed.
- **ZipRecruiter**: blocked by Cloudflare, deliberately on their end.
- **Google Jobs**: returns a JavaScript bot gate with no job data.
- **LinkedIn**: works through JobSpy, but was dropped. Re-add `"linkedin"` to
  `sites` in `searches.toml` if you use it.
- **Adzuna**: an official API, built in and switched off (`[adzuna]` in
  `searches.toml`). It returns only the first 500 characters of each posting,
  which is too little to review or screen.

JobSpy scrapes the job boards. That's subject to their terms of service, and
you use it at your own risk. Keep pulls to a sensible rate: the shipped config
runs 12 queries per pull.

## Privacy

Everything stays on your machine except the model calls. `jobs.db`, `.env`,
`profile/facts.md` and `exports/` are all git-ignored. Screening sends your
facts file and each posting to OpenRouter, which forwards them to Anthropic.
The classify step sends only a job title and the start of its description.

## Where things live

| File | What it does |
|---|---|
| `searches.toml` | Titles, locations, and the job board settings. |
| `filters.toml` | Every kill and flag rule, the gate, and the govcon switch. |
| `profile/facts.md` | Your background: the only thing the screener may believe. |
| `screener/config.py` | Reads the two TOML files into typed config, and builds the queries. |
| `screener/settings.py` | The CLI's edits to those files, comments preserved. |
| `screener/ingest.py` | The only module that knows JobSpy exists. |
| `screener/gate.py` | The relevance gate: is this an IT job at all? |
| `screener/classify.py` | Haiku's yes/no for titles the gate can't place, cached. |
| `screener/filters.py` | The mechanical pass, including the remote check. |
| `screener/screen.py` | The fit score, and the prompt behind it. |
| `screener/llm.py` | The model call, through OpenRouter. |
| `screener/db.py` | SQLite. Killed rows are kept, not discarded. |
| `screener/core.py` | The pipeline. The CLI is a thin wrapper over it. |
| `screener/triage.py` | The `review` loop. |
| `screener/cli.py` | The commands. |

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The filter tests run against the real `filters.toml` and `searches.toml`, not
fixture copies. That way they catch a tuning mistake in the shipped config, not
just a bug in the matching code. If you retune the config for your own search,
expect a few of them to fail, and read what they're guarding before you change
them.

## License

MIT
