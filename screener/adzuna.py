"""Adzuna ingestion.

The second source, added when LinkedIn stopped being usable. Adzuna is an
aggregator with an official API, so there is nothing to scrape and nothing to
break when a page layout changes. Two things about it shape the code below:

- Descriptions come back truncated to 500 characters. The keyword filter only
  ever sees a snippet, so `what_exclude` in searches.toml does the clearance
  kill on Adzuna's side, where the whole posting is searched.
- Locations come back as an area list, ["US", "Virginia", "Arlington County",
  "Arlington"], not a "City, ST" string. The location rule matches tokens like
  "va", so the string is rebuilt here in the shape Indeed uses.
"""

from __future__ import annotations

from typing import Any

import requests

from screener.config import AdzunaConfig, Search
from screener.models import Job

SITE = "adzuna"
API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
TIMEOUT_SECONDS = 30

STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "washington, dc": "DC", "washington dc": "DC",
    "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY",
}


def pull(search: Search, config: AdzunaConfig, results_wanted: int) -> list[Job]:
    """Run one search against Adzuna, paging until `results_wanted` or the end.

    Errors propagate: the caller treats a failed search the same way it treats
    a failed board, reports it and moves on.
    """
    jobs: list[Job] = []
    page = 1
    while len(jobs) < results_wanted:
        ads = _fetch_page(search, config, page)
        jobs.extend(_to_job(ad) for ad in ads)
        if len(ads) < config.results_per_page:
            break
        page += 1
    return jobs[:results_wanted]


def _fetch_page(search: Search, config: AdzunaConfig, page: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "app_id": config.app_id,
        "app_key": config.app_key,
        "what": search.term,
        "where": search.location,
        "distance": config.distance_km,
        "results_per_page": config.results_per_page,
        "max_days_old": config.max_days_old,
        "category": config.category,
        "sort_by": "date",
        "content-type": "application/json",
    }
    if config.what_exclude:
        params["what_exclude"] = config.what_exclude
    url = API.format(country=config.country, page=page)
    response = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
    if not response.ok:
        # Adzuna's error body carries a readable message; the HTTP reason alone
        # ("GONE" for a bad key) is not.
        try:
            detail = response.json().get("display") or response.json().get("exception")
        except ValueError:
            detail = response.reason
        raise RuntimeError(f"Adzuna HTTP {response.status_code}: {detail}")
    return response.json().get("results", [])


def _to_job(ad: dict[str, Any]) -> Job:
    title = _text(ad.get("title"))
    description = _text(ad.get("description"))
    haystack = f"{title} {description}".lower()
    # Adzuna guesses a salary when the ad has none. A guess must not trip the
    # low-pay flag, so predicted salaries are dropped rather than stored.
    predicted = str(ad.get("salary_is_predicted", "0")) == "1"
    salary_min = None if predicted else _number(ad.get("salary_min"))
    salary_max = None if predicted else _number(ad.get("salary_max"))
    return Job(
        title=title,
        company=_text((ad.get("company") or {}).get("display_name")),
        location=_location(ad.get("location") or {}),
        description=description,
        site=SITE,
        # The terms require sending people through this redirect, not to a
        # scraped destination URL.
        url=_text(ad.get("redirect_url")),
        # The same rule the boards use, so the remote check treats it the same way.
        is_remote="remote" in haystack or "work from home" in haystack,
        job_type=_job_type(ad),
        date_posted=_text(ad.get("created"))[:10] or None,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_interval="yearly" if salary_min or salary_max else None,
    )


def _location(location: dict[str, Any]) -> str:
    """"Arlington, VA" from ["US", "Virginia", "Arlington County", "Arlington"].

    `display_name` is "Arlington, Arlington County": no state, so the location
    rule would kill every Adzuna job as out of area.
    """
    area = [_text(part) for part in location.get("area") or [] if _text(part)]
    if len(area) < 2:
        return _text(location.get("display_name"))
    state = area[1]
    # Adzuna writes the District as "Washington, D.C."; the dots and comma have
    # to go before it can be looked up, and it names the city as well.
    code = STATES.get(state.casefold().replace(".", ""), state)
    if code == "DC":
        city = "Washington"
    else:
        city = area[-1] if len(area) > 2 else state
    return f"{city}, {code}"


def _job_type(ad: dict[str, Any]) -> str | None:
    """Folded to JobSpy's vocabulary so the contract flag reads one field."""
    if ad.get("contract_type") == "contract":
        return "contract"
    return {"full_time": "fulltime", "part_time": "parttime"}.get(ad.get("contract_time") or "")


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
